"""Safety checks of the shared fixtures in ``conftest.py``."""

import pytest
from redis.asyncio import Redis

from tests.conftest import ensure_not_dev_db


class TestRedisFixtureGuard:
    @pytest.mark.parametrize(
        "url", ["redis://redis:6379/0", "redis://redis:6379"], ids=["db-0", "no-db"]
    )
    async def test_refuses_dev_db(self, url):
        client = Redis.from_url(url)

        with pytest.raises(pytest.fail.Exception, match="DB 0"):
            ensure_not_dev_db(client)

        await client.aclose()

    async def test_allows_test_db(self):
        client = Redis.from_url("redis://redis:6379/15")

        ensure_not_dev_db(client)

        await client.aclose()

    async def test_starts_with_an_empty_db(self, redis):
        assert await redis.dbsize() == 0
