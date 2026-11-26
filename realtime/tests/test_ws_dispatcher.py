"""Dispatcher map endpoint end to end, through httpx-ws and a real Redis: the
handshake and its close codes, snapshots, several tabs and their limit, messages,
limits, the idle timeout, metrics, and that a watching admin changes nothing.

The app runs in-process; the lifespan does not run under the test transport, so the
broadcaster sits in app.state without its loop and tests tick it themselves. A few
tests run the loop, to see the first snapshot come at once and snapshots keep coming.
WebSocket timeouts are real, shortened to 0.05 s. After every test the driver
invariants must hold.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from prometheus_client import REGISTRY
from starlette.websockets import WebSocketDisconnect as StarletteWebSocketDisconnect
from starlette.websockets import WebSocketState

from app import config
from app.main import app
from app.services.drivers import DriverStore, LocationUpdate, Outcome
from app.services.locations import AcceptedPoint
from app.ws import dispatcher
from app.ws.broadcaster import SnapshotBroadcaster
from app.ws.common import receive_raw
from app.ws.dispatcher import DispatcherSession
from app.ws.manager import ConnectionRegistry
from tests.factories import CENTRE, NOW_MS, make_live_driver
from tests.invariants import assert_driver_invariants

TOKEN = "dev:admin:1"
# ~3 km north of the centre.
NORTH = (48.3191, 25.9358)
SHORT_TIMEOUT_S = 0.05


@pytest.fixture(autouse=True)
def dev_auth(monkeypatch):
    monkeypatch.setattr(config, "DEV_AUTH", True)


@pytest.fixture
def store(redis) -> DriverStore:
    return DriverStore(redis)


@pytest.fixture
def broadcaster(store, clock) -> SnapshotBroadcaster:
    return SnapshotBroadcaster(store, clock)


@pytest.fixture
async def ws_app(redis, store, clock, broadcaster, monkeypatch) -> AsyncIterator[None]:
    """The app with its dependencies in app.state (the lifespan does not run under
    the test transport). At the end: every page unsubscribed and uncounted, and the
    driver invariants hold."""
    dependencies = {
        "redis": redis,
        "drivers": store,
        "driver_connections": ConnectionRegistry(),
        "clock": clock,
        "snapshots": broadcaster,
    }
    for name, value in dependencies.items():
        monkeypatch.setattr(app.state, name, value, raising=False)
    connections = open_admin_connections()
    yield
    # Whatever closed the page (the client, any close code), it let go of everything.
    await until(lambda: broadcaster.subscribers == 0)
    await until(lambda: open_admin_connections() == connections)
    await assert_driver_invariants(redis)


@pytest.fixture
async def snapshot_loop(broadcaster) -> AsyncIterator[None]:
    """The broadcaster's loop in the background, stopped and awaited at the end."""
    stop = asyncio.Event()
    loop = asyncio.create_task(broadcaster.run(stop))
    yield
    stop.set()
    async with asyncio.timeout(1):
        await loop


@asynccontextmanager
async def connect() -> AsyncIterator[AsyncWebSocketSession]:
    """A dispatcher WebSocket to the app in-process; leaving the block waits for the
    server-side handler to finish, cleanup included."""
    async with (
        ASGIWebSocketTransport(app=app) as transport,
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
        aconnect_ws("/rt/ws/dispatcher", http) as ws,
    ):
        yield ws


async def receive(ws) -> dict:
    return await asyncio.wait_for(ws.receive_json(), timeout=2)


async def send(ws, message_type: str, **data) -> None:
    await ws.send_json({"type": message_type, "data": data})


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the event loop until the condition holds (or fail after 1 s)."""
    async with asyncio.timeout(1):
        while not condition():
            await asyncio.sleep(0)


async def auth(ws, broadcaster: SnapshotBroadcaster, *, pages: int = 1) -> None:
    """Authenticate and wait until the page is subscribed (`pages` in total)."""
    await send(ws, "auth", token=TOKEN)
    await until(lambda: broadcaster.subscribers == pages)


async def tick_and_receive(ws, broadcaster: SnapshotBroadcaster) -> dict:
    """One tick of the broadcaster and the snapshot it sent to this page."""
    assert await broadcaster.tick()
    message = await receive(ws)
    assert message["type"] == "dispatcher_snapshot", message
    return message["data"]


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


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def open_admin_connections() -> float:
    return sample("ws_connections", role="admin")


async def dump(redis) -> dict[bytes, bytes]:
    return {key: await redis.dump(key) async for key in redis.scan_iter()}


def positions(snapshot: dict) -> dict[int, tuple[float, float]]:
    return {driver["id"]: (driver["lat"], driver["lon"]) for driver in snapshot["drivers"]}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    async def test_admin_sees_the_drivers_and_their_new_positions(
        self, ws_app, redis, store, broadcaster
    ):
        await make_live_driver(redis, store, driver_id=1, place=CENTRE)
        await make_live_driver(redis, store, driver_id=2, place=CENTRE, tariff="comfort")

        async with connect() as ws:
            await auth(ws, broadcaster)
            first = await tick_and_receive(ws, broadcaster)
            lat, lon = NORTH
            point = AcceptedPoint(lat=lat, lon=lon, ts=NOW_MS + 2000, server_ms=NOW_MS + 2000)
            update = LocationUpdate(point=point, heading=0.0, speed=10.0, in_zone=True)
            assert await store.update_location(1, "conn-1", update) is Outcome.OK

            second = await tick_and_receive(ws, broadcaster)

        assert positions(first) == {1: CENTRE, 2: CENTRE}
        assert first["stats"]["online"] == 2
        assert positions(second) == {1: NORTH, 2: CENTRE}

    async def test_first_snapshot_comes_at_once_after_auth(self, ws_app, snapshot_loop):
        # The real loop with its real 2 s interval: the first page must not wait for it.
        async with connect() as ws:
            await send(ws, "auth", token=TOKEN)

            message = await asyncio.wait_for(ws.receive_json(), timeout=1)

        assert message["type"] == "dispatcher_snapshot"

    async def test_page_that_left_is_unsubscribed(self, ws_app, broadcaster):
        async with connect() as ws:
            await auth(ws, broadcaster)

        await until(lambda: broadcaster.subscribers == 0)

    async def test_watching_admin_changes_nothing_in_redis(self, ws_app, redis, store, broadcaster):
        await make_live_driver(redis, store, driver_id=1)
        await make_live_driver(redis, store, driver_id=2, connected=False)
        before = await dump(redis)

        async with connect() as ws:
            await auth(ws, broadcaster)
            await tick_and_receive(ws, broadcaster)
            await sync(ws)

        assert await dump(redis) == before


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    async def test_nothing_sent_in_time_closes_with_4408(self, ws_app, monkeypatch):
        monkeypatch.setattr(config, "WS_AUTH_TIMEOUT_S", SHORT_TIMEOUT_S)

        async with connect() as ws:
            assert await expect_close(ws) == 4408

    @pytest.mark.parametrize(
        "first_message",
        [
            {"type": "ping", "data": {}},
            {"type": "auth", "data": {"token": "garbage"}},
            {"type": "location", "data": {}},
        ],
        ids=["ping", "garbage token", "driver message"],
    )
    async def test_bad_first_message_closes_with_4401(self, ws_app, first_message):
        async with connect() as ws:
            await ws.send_json(first_message)

            assert await expect_close(ws) == 4401

    @pytest.mark.parametrize("token", ["dev:driver:1", "dev:passenger:1"])
    async def test_other_roles_close_with_4403(self, ws_app, token):
        async with connect() as ws:
            await send(ws, "auth", token=token)

            assert await expect_close(ws) == 4403

    async def test_dev_tokens_are_refused_when_dev_auth_is_off(self, ws_app, monkeypatch):
        monkeypatch.setattr(config, "DEV_AUTH", False)

        async with connect() as ws:
            await send(ws, "auth", token=TOKEN)

            assert await expect_close(ws) == 4401


# ---------------------------------------------------------------------------
# Several tabs
# ---------------------------------------------------------------------------


async def opened(tab: asyncio.Task, is_open: asyncio.Event) -> None:
    """Until the tab in its own task is open; its error, if it failed first."""
    waiting = asyncio.ensure_future(is_open.wait())
    async with asyncio.timeout(2):
        await asyncio.wait({tab, waiting}, return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    if tab.done():
        tab.result()  # raises what the tab failed with


class TestTabs:
    # Each tab gets its own task, as in a browser: a transport cannot be left from a
    # task other than the one that opened it.

    async def test_two_tabs_of_one_admin_both_get_snapshots(self, ws_app, broadcaster):
        first_open = asyncio.Event()

        async def first_tab() -> dict:
            async with connect() as first:
                await auth(first, broadcaster, pages=1)
                first_open.set()
                snapshot = await receive(first)
                await sync(first)
                return snapshot

        first = asyncio.create_task(first_tab())
        await opened(first, first_open)

        async with connect() as second:
            await auth(second, broadcaster, pages=2)
            assert await broadcaster.tick()
            snapshot = await receive(second)
            await sync(second)

        # No takeover (4409): both stayed open and got the same snapshot.
        assert await first == snapshot
        assert snapshot["type"] == "dispatcher_snapshot"

    async def test_tab_above_the_limit_closes_with_1013(self, ws_app, broadcaster, monkeypatch):
        monkeypatch.setattr(config, "DISPATCHER_MAX_CONNECTIONS", 1)
        first_open, second_closed = asyncio.Event(), asyncio.Event()

        async def first_tab() -> None:
            async with connect() as first:
                await auth(first, broadcaster)
                first_open.set()
                await second_closed.wait()
                await sync(first)  # the first tab is untouched

        first = asyncio.create_task(first_tab())
        await opened(first, first_open)

        try:
            async with connect() as second:
                await send(second, "auth", token=TOKEN)

                assert await expect_close(second) == 1013
        finally:
            second_closed.set()
        await first


# ---------------------------------------------------------------------------
# Messages and limits
# ---------------------------------------------------------------------------


class TestMessages:
    async def test_ping_gets_pong(self, ws_app, broadcaster):
        async with connect() as ws:
            await auth(ws, broadcaster)

            await sync(ws)

    async def test_second_auth_is_an_error(self, ws_app, broadcaster):
        async with connect() as ws:
            await auth(ws, broadcaster)

            await send(ws, "auth", token=TOKEN)

            await expect_error(ws, "already_authenticated")
            await sync(ws)

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            (json.dumps({"type": "location", "data": {}}), "unknown_type"),
            ("not json", "invalid_json"),
            (json.dumps({"type": "ping", "data": {"extra": 1}}), "invalid_message"),
        ],
        ids=["driver message", "not json", "invalid data"],
    )
    async def test_bad_message_is_an_error_and_the_connection_lives(
        self, ws_app, broadcaster, raw, code
    ):
        async with connect() as ws:
            await auth(ws, broadcaster)

            await ws.send_text(raw)

            await expect_error(ws, code)
            await sync(ws)

    async def test_snapshots_do_not_restart_the_wait_for_a_message(
        self, ws_app, broadcaster, monkeypatch
    ):
        # A receive cancelled on every wake-up could lose a message on its way in, so
        # the session must keep one receive across snapshots: one call per message.
        calls = 0

        async def counted_receive(websocket):
            nonlocal calls
            calls += 1
            return await receive_raw(websocket)

        monkeypatch.setattr(dispatcher, "receive_raw", counted_receive)
        async with connect() as ws:
            await auth(ws, broadcaster)
            for _ in range(3):
                await tick_and_receive(ws, broadcaster)
            assert calls == 1

            await sync(ws)

            # The one that got the ping, and the next one, now waiting.
            await until(lambda: calls == 2)


class TestLimits:
    async def test_message_over_4_kb_closes_with_1009(self, ws_app, broadcaster):
        async with connect() as ws:
            await auth(ws, broadcaster)

            await ws.send_text("x" * (config.WS_MAX_MESSAGE_BYTES + 1))

            assert await expect_close(ws) == 1009

    async def test_flood_gets_one_rate_limited_error_then_1008(self, ws_app, broadcaster):
        # FakeClock stands still, so no tokens come back during the flood.
        async with connect() as ws:
            await auth(ws, broadcaster)  # takes the first of WS_MSG_BURST tokens
            allowed = config.WS_MSG_BURST - 1
            for _ in range(allowed + config.WS_RATE_LIMIT_CLOSE_AFTER):
                await send(ws, "ping")

            replies = [(await receive(ws))["type"] for _ in range(allowed + 1)]

            assert replies == ["pong"] * allowed + ["error"]
            assert await expect_close(ws) == 1008

    async def test_snapshots_do_not_keep_an_idle_page_open(
        self, ws_app, snapshot_loop, monkeypatch
    ):
        monkeypatch.setattr(config, "WS_IDLE_TIMEOUT_S", 6 * SHORT_TIMEOUT_S)
        monkeypatch.setattr(config, "DISPATCHER_SNAPSHOT_INTERVAL_S", SHORT_TIMEOUT_S / 5)
        snapshots = 0

        async with connect() as ws:
            await send(ws, "auth", token=TOKEN)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                while True:
                    assert (await receive(ws))["type"] == "dispatcher_snapshot"
                    snapshots += 1

        assert exc_info.value.code == 4000
        # Snapshots kept coming the whole time: they did not move the deadline.
        assert snapshots >= 2


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    async def test_open_pages_are_counted_after_auth(self, ws_app, broadcaster):
        before = open_admin_connections()

        async with connect() as ws:
            await auth(ws, broadcaster)
            during = open_admin_connections()

        assert during == before + 1
        await until(lambda: open_admin_connections() == before)

    async def test_close_before_auth_is_counted_with_unknown_role(self, ws_app, monkeypatch):
        monkeypatch.setattr(config, "WS_AUTH_TIMEOUT_S", SHORT_TIMEOUT_S)
        before = sample("ws_closes_total", role="unknown", code="4408")

        async with connect() as ws:
            await expect_close(ws)

        assert sample("ws_closes_total", role="unknown", code="4408") == before + 1

    async def test_close_after_auth_is_counted_with_the_admin_role(
        self, ws_app, broadcaster, monkeypatch
    ):
        monkeypatch.setattr(config, "DISPATCHER_MAX_CONNECTIONS", 0)
        before = sample("ws_closes_total", role="admin", code="1013")
        connections = open_admin_connections()

        async with connect() as ws:
            await send(ws, "auth", token=TOKEN)
            await expect_close(ws)

        assert sample("ws_closes_total", role="admin", code="1013") == before + 1
        # Counted after auth, so taken back on the way out too.
        await until(lambda: open_admin_connections() == connections)


# ---------------------------------------------------------------------------
# Sessions on a stand-in socket
# ---------------------------------------------------------------------------


class SnapshotSocket:
    """An admin page on a stand-in socket: authenticates, sends `frames`, then says
    nothing more. Sending raises `send_fails`, or waits forever if it is None (a link
    that takes nothing). The close frame raises `close_fails` if given, or waits
    forever if `close_hangs` (as on a real stuck link, where it queues behind the
    snapshots)."""

    application_state = WebSocketState.CONNECTED
    client_state = WebSocketState.CONNECTED

    def __init__(
        self,
        *,
        send_fails: BaseException | None = None,
        close_hangs: bool = False,
        close_fails: BaseException | None = None,
        frames: tuple[str, ...] = (),
    ):
        self._send_fails = send_fails
        self._close_hangs = close_hangs
        self._close_fails = close_fails
        self._frames = [json.dumps({"type": "auth", "data": {"token": TOKEN}}), *frames]
        self.sends = 0
        self.closed_with: int | None = None

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict:
        if self._frames:
            return {"type": "websocket.receive", "text": self._frames.pop(0)}
        await asyncio.Event().wait()  # the page says nothing more
        raise AssertionError("unreachable")  # pragma: no cover

    async def send_text(self, text: str) -> None:
        self.sends += 1
        if self._send_fails is not None:
            raise self._send_fails
        await asyncio.Event().wait()

    async def close(self, code: int, reason: str) -> None:
        self.closed_with = code
        if self._close_fails is not None:
            raise self._close_fails
        if self._close_hangs:
            await asyncio.Event().wait()


async def run_until_the_first_snapshot_is_sent(socket, broadcaster, clock) -> None:
    session = asyncio.create_task(DispatcherSession(socket, broadcaster, clock).run())
    await until(lambda: broadcaster.subscribers == 1)

    assert await broadcaster.tick()

    async with asyncio.timeout(1):
        await session


class TestSessionEnds:
    async def test_page_that_vanished_mid_send_is_cleaned_up(self, broadcaster, clock):
        connections = open_admin_connections()
        socket = SnapshotSocket(send_fails=StarletteWebSocketDisconnect(1006))

        await run_until_the_first_snapshot_is_sent(socket, broadcaster, clock)

        assert broadcaster.subscribers == 0
        assert open_admin_connections() == connections
        assert socket.closed_with is None  # nobody to send a close frame to

    async def test_unexpected_error_closes_with_1011_and_is_logged(
        self, broadcaster, clock, caplog
    ):
        before = sample("ws_closes_total", role="admin", code="1011")
        socket = SnapshotSocket(send_fails=RuntimeError("bug"))

        await run_until_the_first_snapshot_is_sent(socket, broadcaster, clock)

        assert socket.closed_with == 1011
        assert sample("ws_closes_total", role="admin", code="1011") == before + 1
        assert "Dispatcher map session of admin 1 failed" in caplog.text
        assert broadcaster.subscribers == 0


@pytest.fixture
def short_send_timeouts(monkeypatch):
    monkeypatch.setattr(config, "DISPATCHER_SEND_TIMEOUT_S", SHORT_TIMEOUT_S)
    monkeypatch.setattr(config, "DISPATCHER_CLOSE_TIMEOUT_S", SHORT_TIMEOUT_S)


class TestSlowPages:
    async def test_page_that_does_not_take_a_snapshot_is_closed_with_1013(
        self, broadcaster, clock, short_send_timeouts, caplog
    ):
        before = sample("ws_closes_total", role="admin", code="1013")
        connections = open_admin_connections()
        socket = SnapshotSocket()

        await run_until_the_first_snapshot_is_sent(socket, broadcaster, clock)

        assert socket.closed_with == 1013
        assert sample("ws_closes_total", role="admin", code="1013") == before + 1
        assert "does not keep up" in caplog.text
        assert broadcaster.subscribers == 0
        assert open_admin_connections() == connections

    async def test_stuck_page_does_not_hold_up_the_others(self, broadcaster, clock):
        # The real 5 s send timeout: the stuck page stays stuck for the whole test.
        socket = SnapshotSocket()
        stuck = asyncio.create_task(DispatcherSession(socket, broadcaster, clock).run())
        await until(lambda: broadcaster.subscribers == 1)
        other = broadcaster.subscribe()
        try:
            assert await broadcaster.tick()
            await until(lambda: socket.sends == 1)  # the stuck page is mid-send now

            for _ in range(3):
                async with asyncio.timeout(1):
                    # Neither the tick nor the other page waits for the stuck one.
                    assert await broadcaster.tick()
                    await other.wait()
            # Still stuck in its first send: the real timeout was in effect.
            assert not stuck.done()
            assert socket.closed_with is None
        finally:
            broadcaster.unsubscribe(other)
            stuck.cancel()
            await asyncio.gather(stuck, return_exceptions=True)
        assert socket.sends == 1  # its newer snapshots only replaced each other
        # A session cancelled mid-send (e.g. on shutdown) lets go of its slot too.
        assert broadcaster.subscribers == 0

    async def test_page_that_takes_no_close_frame_either_still_ends(
        self, broadcaster, clock, short_send_timeouts, caplog
    ):
        socket = SnapshotSocket(close_hangs=True)

        await run_until_the_first_snapshot_is_sent(socket, broadcaster, clock)

        assert socket.closed_with == 1013
        assert "did not take the close frame in time" in caplog.text
        assert broadcaster.subscribers == 0

    async def test_page_that_does_not_take_a_pong_is_closed_with_1013(
        self, broadcaster, clock, short_send_timeouts
    ):
        # Every send has the timeout, not only the snapshots.
        socket = SnapshotSocket(frames=(json.dumps({"type": "ping", "data": {}}),))

        session = asyncio.create_task(DispatcherSession(socket, broadcaster, clock).run())
        async with asyncio.timeout(1):
            await session

        assert socket.closed_with == 1013
        assert socket.sends == 1

    async def test_bug_with_a_close_that_hangs_still_ends_the_session(
        self, broadcaster, clock, short_send_timeouts, caplog
    ):
        socket = SnapshotSocket(send_fails=RuntimeError("bug"), close_hangs=True)

        await run_until_the_first_snapshot_is_sent(socket, broadcaster, clock)

        assert socket.closed_with == 1011
        assert "did not take the close frame in time" in caplog.text
        assert broadcaster.subscribers == 0

    async def test_timeout_error_from_the_socket_itself_is_a_bug_not_a_slow_page(
        self, broadcaster, clock, short_send_timeouts
    ):
        # Only the session's own limit means "too slow": anything else must show.
        socket = SnapshotSocket(send_fails=TimeoutError("from the transport"))

        await run_until_the_first_snapshot_is_sent(socket, broadcaster, clock)

        assert socket.closed_with == 1011

    async def test_timeout_error_from_the_close_itself_is_not_swallowed(
        self, broadcaster, clock, short_send_timeouts
    ):
        socket = SnapshotSocket(close_fails=TimeoutError("from the transport"))
        session = asyncio.create_task(DispatcherSession(socket, broadcaster, clock).run())
        await until(lambda: broadcaster.subscribers == 1)
        assert await broadcaster.tick()

        with pytest.raises(TimeoutError, match="from the transport"):
            async with asyncio.timeout(1):
                await session

        assert broadcaster.subscribers == 0
