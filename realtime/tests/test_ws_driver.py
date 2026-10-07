"""Driver WebSocket endpoint end to end, through httpx-ws and a real Redis: handshake
and its close codes, the takeover by a second connection, disconnects, every message
type, limits, Redis failures and metrics.

The app runs in-process, in the test's event loop. Time that the checks depend on
comes from FakeClock; only the WebSocket timeouts are real, shortened to 0.05 s.
After every test the driver invariants (tests/invariants.py) must hold.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.websockets import WebSocketDisconnect as StarletteWebSocketDisconnect
from starlette.websockets import WebSocketState

from app import config, redis_keys
from app.main import app
from app.schemas.ws import CloseCode
from app.services.drivers import DriverStore
from app.ws.driver import DriverSession
from app.ws.manager import ConnectionRegistry
from tests.factories import make_driver_profile
from tests.invariants import assert_driver_invariants

DRIVER = 1
TOKEN = "dev:driver:1"
CENTRE = (48.2921, 25.9358)
KYIV = (50.4501, 30.5234)
# ~3 km north of the centre.
NORTH_3_KM = (48.3191, 25.9358)
SHORT_TIMEOUT_S = 0.05


@pytest.fixture(autouse=True)
def dev_auth(monkeypatch):
    monkeypatch.setattr(config, "DEV_AUTH", True)


@pytest.fixture
def store(redis) -> DriverStore:
    return DriverStore(redis)


@pytest.fixture
async def ws_app(redis, store, clock, monkeypatch) -> AsyncIterator[None]:
    """The app with its dependencies in app.state; the lifespan that would create them
    does not run under the test transport. Checks the invariants at the end."""
    dependencies = {
        "redis": redis,
        "drivers": store,
        "driver_connections": ConnectionRegistry(),
        "clock": clock,
    }
    for name, value in dependencies.items():
        monkeypatch.setattr(app.state, name, value, raising=False)
    yield
    await assert_driver_invariants(redis)


@pytest.fixture
async def profile(redis) -> int:
    return await make_driver_profile(redis, driver_id=DRIVER)


@asynccontextmanager
async def connect() -> AsyncIterator[AsyncWebSocketSession]:
    """A driver WebSocket to the app in-process.

    Each connection gets its own transport, opened and closed inside the test: its
    task group cannot span a fixture's setup and teardown, which pytest-asyncio runs
    in different tasks. Leaving the block waits for the server-side handler to
    finish, cleanup included.
    """
    async with (
        ASGIWebSocketTransport(app=app) as transport,
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        aconnect_ws("/rt/ws/driver", http) as ws,
    ):
        yield ws


async def receive(ws) -> dict:
    return await asyncio.wait_for(ws.receive_json(), timeout=2)


async def send(ws, message_type: str, **data) -> None:
    await ws.send_json({"type": message_type, "data": data})


async def auth(ws, token: str = TOKEN) -> dict:
    """Authenticate and return the state_snapshot data."""
    await send(ws, "auth", token=token)
    message = await receive(ws)
    assert message["type"] == "state_snapshot"
    return message["data"]


async def go_online(ws) -> dict:
    await send(ws, "go_online")
    message = await receive(ws)
    assert message["type"] == "state_snapshot"
    return message["data"]


async def send_location(ws, clock, place=CENTRE, **data) -> None:
    lat, lon = place
    fields = {"lat": lat, "lon": lon, "heading": 90.0, "speed": 10.0, "ts": clock.now_ms()}
    await send(ws, "location", **(fields | data))


async def sync(ws) -> None:
    """Ping and wait for the pong: every message sent before it has been handled."""
    await send(ws, "ping")
    assert await receive(ws) == {"type": "pong", "data": {}}


async def expect_error(ws, code: str) -> None:
    message = await receive(ws)
    assert message["type"] == "error", message
    assert message["data"]["code"] == code


async def expect_close(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        while True:
            await receive(ws)
    return exc_info.value.code


async def online_driver(ws, clock) -> None:
    """Authenticate, go online and send an accepted point in the centre."""
    await auth(ws)
    await go_online(ws)
    await send_location(ws, clock)
    await sync(ws)


async def drivers_near(redis, place=CENTRE, tariff="economy") -> list[int]:
    lat, lon = place
    found = await redis.geosearch(
        redis_keys.drivers_geo(tariff), longitude=lon, latitude=lat, radius=100, unit="m"
    )
    return [int(member) for member in found]


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def open_driver_connections() -> float:
    return sample("ws_connections", role="driver")


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    async def test_driver_goes_online_and_is_found_near_their_point(
        self, ws_app, redis, profile, clock
    ):
        async with connect() as ws:
            snapshot = await auth(ws)
            assert snapshot == {
                "status": "offline",
                "tariff": "economy",
                "in_zone": False,
                "position": None,
            }

            assert (await go_online(ws))["status"] == "available"
            await send_location(ws, clock)
            await sync(ws)

            assert await drivers_near(redis) == [DRIVER]

    async def test_reconnecting_driver_gets_their_last_position(self, ws_app, profile, clock):
        async with connect() as ws:
            await online_driver(ws, clock)

        async with connect() as ws:
            snapshot = await auth(ws)

        assert snapshot["status"] == "available"
        assert snapshot["in_zone"] is True
        assert snapshot["position"] == {
            "lat": CENTRE[0],
            "lon": CENTRE[1],
            "heading": 90.0,
            "speed": 10.0,
        }

    async def test_nothing_sent_in_time_closes_with_4408(self, ws_app, profile, monkeypatch):
        monkeypatch.setattr(config, "WS_AUTH_TIMEOUT_S", SHORT_TIMEOUT_S)

        async with connect() as ws:
            assert await expect_close(ws) == 4408

    @pytest.mark.parametrize(
        "first_message",
        [
            {"type": "ping", "data": {}},
            {"type": "auth", "data": {"token": "garbage"}},
            {"type": "auth", "data": {}},
        ],
        ids=["not-auth", "garbage-token", "no-token"],
    )
    async def test_bad_first_message_closes_with_4401(self, ws_app, profile, first_message):
        async with connect() as ws:
            await ws.send_json(first_message)

            assert await expect_close(ws) == 4401

    async def test_invalid_json_first_closes_with_4401(self, ws_app, profile):
        async with connect() as ws:
            await ws.send_text("{")

            assert await expect_close(ws) == 4401

    async def test_dev_tokens_are_refused_when_dev_auth_is_off(self, ws_app, profile, monkeypatch):
        monkeypatch.setattr(config, "DEV_AUTH", False)

        async with connect() as ws:
            await send(ws, "auth", token=TOKEN)

            assert await expect_close(ws) == 4401

    async def test_passenger_closes_with_4403(self, ws_app, profile):
        async with connect() as ws:
            await send(ws, "auth", token="dev:passenger:1")

            assert await expect_close(ws) == 4403

    async def test_driver_without_profile_closes_with_4403(self, ws_app):
        async with connect() as ws:
            await send(ws, "auth", token=TOKEN)

            assert await expect_close(ws) == 4403

    async def test_ineligible_driver_closes_with_4403(self, ws_app, redis):
        await make_driver_profile(redis, driver_id=DRIVER, eligible=False)

        async with connect() as ws:
            await send(ws, "auth", token=TOKEN)

            assert await expect_close(ws) == 4403

    async def test_closes_before_auth_are_counted_with_unknown_role(self, ws_app, profile):
        before = sample("ws_closes_total", role="unknown", code="4401")

        async with connect() as ws:
            await send(ws, "auth", token="garbage")
            await expect_close(ws)

        assert sample("ws_closes_total", role="unknown", code="4401") == before + 1

    async def test_open_connections_are_counted_after_auth(self, ws_app, profile):
        before = open_driver_connections()

        async with connect() as ws:
            await auth(ws)
            assert open_driver_connections() == before + 1

        assert open_driver_connections() == before


# ---------------------------------------------------------------------------
# Second connection and disconnects
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    async def test_second_connection_closes_the_first_with_4409(
        self, ws_app, redis, profile, clock
    ):
        first_online = asyncio.Event()

        async def first_phone() -> int:
            # A client of its own, as in reality: its transport lives in this task.
            async with connect() as first:
                await online_driver(first, clock)
                first_online.set()
                return await expect_close(first)

        first = asyncio.create_task(first_phone())
        await first_online.wait()

        async with connect() as second:
            await auth(second)

            # Leaving connect() waited for the first handler's cleanup, which ran with
            # a conn_id that is no longer the driver's: nothing was removed.
            assert await first == 4409
            assert await drivers_near(redis) == [DRIVER]

            clock.advance(2)
            await send_location(second, clock)
            await sync(second)
            assert await drivers_near(redis) == [DRIVER]

    async def test_disconnect_leaves_geo_but_keeps_the_driver_available(
        self, ws_app, redis, store, profile, clock
    ):
        before = open_driver_connections()
        async with connect() as ws:
            await online_driver(ws, clock)

        assert open_driver_connections() == before
        assert await drivers_near(redis) == []
        assert (await store.get_state(DRIVER)).status == "available"

    async def test_reconnect_and_a_point_return_the_driver_to_geo(
        self, ws_app, redis, profile, clock
    ):
        async with connect() as ws:
            await online_driver(ws, clock)
        clock.advance(5)

        async with connect() as ws:
            await auth(ws)
            await send_location(ws, clock)
            await sync(ws)

            assert await drivers_near(redis) == [DRIVER]

    @pytest.mark.parametrize("message_type", ["location", "go_online", "go_offline"])
    async def test_connection_taken_over_elsewhere_closes_with_4409(
        self, ws_app, redis, profile, clock, message_type
    ):
        # Another gateway instance bound the driver (stage 5 will tell this one directly).
        async with connect() as ws:
            await online_driver(ws, clock)
            await redis.hset(redis_keys.driver_state(DRIVER), "conn_id", "elsewhere")
            clock.advance(2)

            if message_type == "location":
                await send_location(ws, clock)
            else:
                await send(ws, message_type)

            assert await expect_close(ws) == 4409

    async def test_location_from_a_connection_taken_over_elsewhere_is_counted(
        self, ws_app, redis, profile, clock
    ):
        before = sample("location_updates_total", result="rejected_stale_connection")
        async with connect() as ws:
            await online_driver(ws, clock)
            await redis.hset(redis_keys.driver_state(DRIVER), "conn_id", "elsewhere")
            clock.advance(2)

            await send_location(ws, clock)
            await expect_close(ws)

        after = sample("location_updates_total", result="rejected_stale_connection")
        assert after == before + 1


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class TestMessages:
    async def test_ping_gets_pong(self, ws_app, profile):
        async with connect() as ws:
            await auth(ws)

            await sync(ws)

    async def test_binary_frame_is_handled_like_text(self, ws_app, profile):
        async with connect() as ws:
            await auth(ws)

            await ws.send_bytes(b'{"type": "ping", "data": {}}')

            assert (await receive(ws))["type"] == "pong"

    async def test_second_auth_is_an_error(self, ws_app, profile):
        async with connect() as ws:
            await auth(ws)

            await send(ws, "auth", token=TOKEN)

            await expect_error(ws, "already_authenticated")

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("{", "invalid_json"),
            ('{"type": "offer_response", "data": {}}', "unknown_type"),
            ('{"type": "ping", "data": {"x": 1}}', "invalid_message"),
        ],
    )
    async def test_bad_message_is_an_error_and_the_connection_lives(
        self, ws_app, profile, raw, code
    ):
        async with connect() as ws:
            await auth(ws)

            await ws.send_text(raw)

            await expect_error(ws, code)
            await sync(ws)

    async def test_go_offline_returns_a_snapshot(self, ws_app, redis, profile, clock):
        async with connect() as ws:
            await online_driver(ws, clock)

            await send(ws, "go_offline")

            assert (await receive(ws))["data"]["status"] == "offline"
            assert await drivers_near(redis) == []

    async def test_go_offline_while_offline_is_not_online_with_a_snapshot(self, ws_app, profile):
        async with connect() as ws:
            await auth(ws)

            await send(ws, "go_offline")

            await expect_error(ws, "not_online")
            assert (await receive(ws))["data"]["status"] == "offline"

    async def test_go_online_after_losing_eligibility_is_an_error(self, ws_app, redis, profile):
        async with connect() as ws:
            await auth(ws)
            await redis.hset(redis_keys.driver_profile(DRIVER), "eligible", 0)

            await send(ws, "go_online")

            await expect_error(ws, "not_eligible")

    @pytest.mark.parametrize("message_type", ["go_online", "go_offline"])
    async def test_status_change_during_a_trip_is_busy(
        self, ws_app, redis, profile, clock, message_type
    ):
        async with connect() as ws:
            await online_driver(ws, clock)
            # Stage 3 sets on_trip; until then it is written by hand.
            await redis.hset(redis_keys.driver_state(DRIVER), "status", "on_trip")
            await redis.zrem(redis_keys.drivers_geo("economy"), DRIVER)

            await send(ws, message_type)

            await expect_error(ws, "busy")


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


class TestLocations:
    async def test_accepted_point_is_counted(self, ws_app, profile, clock):
        before = sample("location_updates_total", result="accepted")

        async with connect() as ws:
            await online_driver(ws, clock)

        assert sample("location_updates_total", result="accepted") == before + 1

    async def test_point_out_of_zone_is_accepted_but_not_offerable(
        self, ws_app, redis, store, profile, clock
    ):
        before = sample("location_updates_total", result="out_of_zone")
        async with connect() as ws:
            await online_driver(ws, clock)
            clock.advance(60)  # a long pause: no jump check

            await send_location(ws, clock, KYIV)
            await sync(ws)

            assert await drivers_near(redis) == []
            assert (await store.get_state(DRIVER)).in_zone is False
        assert sample("location_updates_total", result="out_of_zone") == before + 1

    async def test_point_from_offline_driver_is_not_online_with_a_snapshot(
        self, ws_app, profile, clock
    ):
        before = sample("location_updates_total", result="rejected_offline")
        async with connect() as ws:
            await auth(ws)

            await send_location(ws, clock)

            await expect_error(ws, "not_online")
            assert (await receive(ws))["type"] == "state_snapshot"
        assert sample("location_updates_total", result="rejected_offline") == before + 1

    async def test_driver_reaped_while_connected_learns_it_from_the_next_point(
        self, ws_app, store, profile, clock
    ):
        async with connect() as ws:
            await online_driver(ws, clock)
            clock.advance(config.DRIVER_SILENCE_S + 1)
            cutoff = clock.now_ms() - config.DRIVER_SILENCE_S * 1000
            assert await store.reap(DRIVER, cutoff) == "reaped"

            await send_location(ws, clock)

            await expect_error(ws, "not_online")
            assert (await receive(ws))["data"]["status"] == "offline"

    async def test_point_within_a_second_is_throttled_silently(self, ws_app, profile, clock):
        before = sample("location_updates_total", result="throttled")
        async with connect() as ws:
            await online_driver(ws, clock)
            clock.advance(0.5)

            await send_location(ws, clock)

            await sync(ws)  # the next reply is the pong: no error for a throttled point
        assert sample("location_updates_total", result="throttled") == before + 1

    @pytest.mark.parametrize(
        ("pause_s", "lag_ms", "place", "code", "result"),
        [
            # 3 km in 2 s.
            (2, 0, NORTH_3_KM, "location_jump", "rejected_jump"),
            # Recorded 20 s before it arrived.
            (30, 20_000, CENTRE, "location_stale", "rejected_stale"),
        ],
        ids=["jump", "stale"],
    )
    async def test_implausible_point_is_an_error_and_counted(
        self, ws_app, profile, clock, pause_s, lag_ms, place, code, result
    ):
        before = sample("location_updates_total", result=result)
        async with connect() as ws:
            await online_driver(ws, clock)
            clock.advance(pause_s)

            await send_location(ws, clock, place, ts=clock.now_ms() - lag_ms)

            await expect_error(ws, code)
        assert sample("location_updates_total", result=result) == before + 1

    async def test_point_older_than_the_previous_one_is_out_of_order(self, ws_app, profile, clock):
        async with connect() as ws:
            await online_driver(ws, clock)
            clock.advance(2)

            await send_location(ws, clock, ts=clock.now_ms() - 10_000)

            await expect_error(ws, "location_out_of_order")

    async def test_invalid_point_is_counted(self, ws_app, profile, clock):
        before = sample("location_updates_total", result="rejected_invalid")
        async with connect() as ws:
            await auth(ws)

            await send_location(ws, clock, heading=360)

            await expect_error(ws, "invalid_message")
        assert sample("location_updates_total", result="rejected_invalid") == before + 1

    async def test_point_without_heading_and_speed_is_accepted(self, ws_app, redis, profile, clock):
        async with connect() as ws:
            await auth(ws)
            await go_online(ws)

            await send_location(ws, clock, heading=None, speed=None)
            await sync(ws)

            assert await drivers_near(redis) == [DRIVER]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


class TestLimits:
    async def test_message_over_4_kb_closes_with_1009(self, ws_app, profile):
        async with connect() as ws:
            await auth(ws)

            await ws.send_text("x" * (config.WS_MAX_MESSAGE_BYTES + 1))

            assert await expect_close(ws) == 1009

    async def test_message_of_exactly_4_kb_is_read(self, ws_app, profile):
        async with connect() as ws:
            await auth(ws)
            padding = config.WS_MAX_MESSAGE_BYTES - len('{"type": "ping", "data": {}}')

            await ws.send_text('{"type": "ping", "data": {}}' + " " * padding)

            assert (await receive(ws))["type"] == "pong"

    async def test_flood_gets_one_rate_limited_error_then_1008(self, ws_app, profile):
        # FakeClock stands still, so no tokens come back during the flood.
        async with connect() as ws:
            await auth(ws)  # takes the first of WS_MSG_BURST tokens
            allowed = config.WS_MSG_BURST - 1
            for _ in range(allowed + config.WS_RATE_LIMIT_CLOSE_AFTER):
                await send(ws, "ping")

            replies = [(await receive(ws))["type"] for _ in range(allowed + 1)]

            assert replies == ["pong"] * allowed + ["error"]
            assert await expect_close(ws) == 1008

    async def test_idle_connection_closes_with_4000(self, ws_app, profile, monkeypatch):
        monkeypatch.setattr(config, "WS_IDLE_TIMEOUT_S", SHORT_TIMEOUT_S)

        async with connect() as ws:
            await auth(ws)

            assert await expect_close(ws) == 4000


# ---------------------------------------------------------------------------
# Redis failures
# ---------------------------------------------------------------------------


async def redis_down(*args, **kwargs):
    raise RedisConnectionError("Redis is down")


class TestRedisFailures:
    async def test_redis_error_while_handling_a_message_closes_with_1011(
        self, ws_app, store, profile, monkeypatch
    ):
        async with connect() as ws:
            await auth(ws)
            monkeypatch.setattr(store, "go_online", redis_down)

            await send(ws, "go_online")

            assert await expect_close(ws) == 1011

    async def test_redis_error_during_cleanup_is_only_logged(
        self, ws_app, store, profile, clock, monkeypatch, caplog
    ):
        before = open_driver_connections()
        async with connect() as ws:
            await auth(ws)
            monkeypatch.setattr(store, "disconnect", redis_down)

        assert open_driver_connections() == before
        assert "disconnect not recorded" in caplog.text


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------


class ClientGoneWebSocket:
    """A socket whose client vanished just before the server's close frame."""

    application_state = WebSocketState.CONNECTED
    client_state = WebSocketState.CONNECTED

    async def close(self, code: int, reason: str) -> None:
        raise StarletteWebSocketDisconnect(1006)


async def test_close_tolerates_a_client_that_left_at_the_same_moment(store, clock):
    session = DriverSession(ClientGoneWebSocket(), store, ConnectionRegistry(), clock)

    await session._close(CloseCode.IDLE, "no messages for too long")
