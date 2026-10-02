import httpx
import pytest
import redis.asyncio as redis

from app import config
from app.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_returns_ok_when_redis_is_up(client):
    app.state.redis = redis.from_url(config.REDIS_URL)
    try:
        response = await client.get("/rt/health")
    finally:
        await app.state.redis.aclose()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "up"}


async def test_health_returns_503_when_redis_is_down(client):
    app.state.redis = redis.from_url("redis://invalid-host:6379/0", socket_connect_timeout=1)
    try:
        response = await client.get("/rt/health")
    finally:
        await app.state.redis.aclose()

    assert response.status_code == 503
    assert response.json() == {"status": "error", "redis": "down"}
