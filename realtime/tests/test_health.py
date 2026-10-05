"""Health check of the realtime service: Redis is critical, OSRM only degrades routing."""

import httpx
import pytest
from redis.asyncio import Redis

from app.main import app
from app.services.osrm import OsrmClient


def osrm_answering(response: httpx.Response) -> OsrmClient:
    transport = httpx.MockTransport(lambda request: response)
    return OsrmClient(httpx.AsyncClient(transport=transport, base_url="http://osrm"))


def osrm_down() -> OsrmClient:
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(refuse)
    return OsrmClient(httpx.AsyncClient(transport=transport, base_url="http://osrm"))


OSRM_UP = httpx.Response(200, json={"code": "Ok", "waypoints": [{"location": [25.9, 48.3]}]})


@pytest.fixture
def redis_up(redis):
    app.state.redis = redis


@pytest.fixture
async def redis_down():
    app.state.redis = Redis.from_url("redis://invalid-host:6379/0", socket_connect_timeout=1)
    yield
    await app.state.redis.aclose()


async def test_health_reports_all_up(client, redis_up):
    app.state.osrm = osrm_answering(OSRM_UP)

    response = await client.get("/rt/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "up", "osrm": "up"}


async def test_health_is_ok_when_only_osrm_is_down(client, redis_up):
    app.state.osrm = osrm_down()

    response = await client.get("/rt/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "up", "osrm": "down"}


async def test_osrm_is_up_when_it_answers_without_a_road(client, redis_up):
    app.state.osrm = osrm_answering(httpx.Response(400, json={"code": "NoSegment"}))

    response = await client.get("/rt/health")

    assert response.json()["osrm"] == "up"


async def test_health_returns_503_when_redis_is_down(client, redis_down):
    app.state.osrm = osrm_answering(OSRM_UP)

    response = await client.get("/rt/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "redis": "down", "osrm": "up"}
