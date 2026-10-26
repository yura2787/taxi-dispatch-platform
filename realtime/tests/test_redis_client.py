"""The Redis client of the realtime processes: a burst of commands larger than the
connection pool waits for free connections instead of failing at once."""

import asyncio
import time

import pytest
from redis.asyncio import BlockingConnectionPool
from redis.exceptions import RedisError

from app import config
from app.redis_client import create_redis


@pytest.fixture
async def make_client(redis, monkeypatch):
    """Clients with a pool of 2 connections to the test DB (`redis` empties it)."""
    monkeypatch.setattr(config, "REDIS_MAX_CONNECTIONS", 2)
    clients = []

    def make():
        client = create_redis()
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


class TestPool:
    async def test_pool_is_bounded_and_blocking(self, make_client):
        client = make_client()

        pool = client.connection_pool

        assert isinstance(pool, BlockingConnectionPool)
        assert pool.max_connections == 2

    async def test_burst_larger_than_the_pool_waits_for_free_connections(self, make_client):
        client = make_client()
        # Each BLPOP holds its connection for 0.1 s: ten through two connections
        # take five rounds, so they must have queued for a free connection.
        burst = [client.blpop(f"empty:{n}", timeout=0.1) for n in range(10)]
        started = time.monotonic()

        results = await asyncio.gather(*burst)

        assert results == [None] * 10
        assert time.monotonic() - started >= 0.4

    async def test_waiting_longer_than_the_pool_timeout_is_a_redis_error(
        self, make_client, monkeypatch
    ):
        # Still a RedisError, so the WebSocket handler closes with 1011 as before.
        monkeypatch.setattr(config, "REDIS_POOL_TIMEOUT_S", 0.05)
        client = make_client()
        holders = asyncio.gather(*(client.blpop(f"empty:{n}", timeout=0.3) for n in range(2)))
        for _ in range(5):
            await asyncio.sleep(0)  # the holders take both connections

        try:
            with pytest.raises(RedisError, match="No connection available"):
                await client.ping()
        finally:
            await holders
