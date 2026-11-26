"""App wiring: what the lifespan creates for the request and WebSocket handlers, and
that it stops the snapshot loop and closes the clients it created however the app
stops, the loop first."""

import asyncio
import logging

import httpx
import pytest

from app import config, main
from app.clock import Clock
from app.main import app, lifespan
from app.redis_client import create_redis
from app.services.drivers import DriverStore
from app.services.osrm import OsrmClient
from app.ws.broadcaster import SnapshotBroadcaster
from app.ws.manager import ConnectionRegistry

STATE = ("redis", "osrm", "drivers", "driver_connections", "clock", "snapshots")


class AppCrashed(Exception):
    """The app stopping (or failing to start) with an error."""


class CloseFailed(Exception):
    """A client failing to close."""


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    # Other tests put their own objects into app.state; keep them out of these.
    for name in STATE:
        monkeypatch.setattr(app.state, name, None, raising=False)


@pytest.fixture
def closed(monkeypatch) -> list[str]:
    """What the lifespan has stopped or closed, in order: "snapshots" (the loop),
    "osrm", "redis"."""
    names: list[str] = []

    def create_tracked_redis():
        redis = create_redis()
        close = redis.aclose

        async def aclose() -> None:
            names.append("redis")
            await close()

        monkeypatch.setattr(redis, "aclose", aclose)
        return redis

    class TrackedOsrmHttp(httpx.AsyncClient):
        async def aclose(self) -> None:
            names.append("osrm")
            await super().aclose()

    class TrackedBroadcaster(SnapshotBroadcaster):
        async def run(self, stop: asyncio.Event) -> None:
            try:
                await super().run(stop)
            finally:
                names.append("snapshots")

    monkeypatch.setattr(main, "create_redis", create_tracked_redis)
    monkeypatch.setattr(main, "SnapshotBroadcaster", TrackedBroadcaster)
    # main uses httpx.AsyncClient only for OSRM.
    monkeypatch.setattr(main.httpx, "AsyncClient", TrackedOsrmHttp)
    return names


def fail_to_close_osrm(monkeypatch) -> None:
    """The OSRM HTTP client closes (still tracked), then raises CloseFailed."""
    tracked = main.httpx.AsyncClient

    class FailingOsrmHttp(tracked):
        async def aclose(self) -> None:
            await super().aclose()
            raise CloseFailed

    monkeypatch.setattr(main.httpx, "AsyncClient", FailingOsrmHttp)


def crash(*args, **kwargs):
    raise AppCrashed


class TestStartup:
    async def test_lifespan_creates_shared_dependencies(self):
        async with lifespan(app):
            assert isinstance(app.state.osrm, OsrmClient)
            assert isinstance(app.state.drivers, DriverStore)
            assert isinstance(app.state.driver_connections, ConnectionRegistry)
            assert isinstance(app.state.clock, Clock)
            assert isinstance(app.state.snapshots, SnapshotBroadcaster)
            assert await app.state.redis.ping()


class TestShutdown:
    async def test_normal_shutdown_closes_every_client(self, closed):
        async with lifespan(app):
            assert closed == []

        # Reverse order of creation: the loop stops before the clients it uses close.
        assert closed == ["snapshots", "osrm", "redis"]

    async def test_app_stopping_with_an_exception_still_closes_every_client(self, closed):
        with pytest.raises(AppCrashed):
            async with lifespan(app):
                raise AppCrashed

        assert closed == ["snapshots", "osrm", "redis"]

    async def test_startup_failing_halfway_closes_what_was_created(self, closed, monkeypatch):
        monkeypatch.setattr(main, "OsrmClient", crash)

        with pytest.raises(AppCrashed):
            async with lifespan(app):
                pass  # pragma: no cover - startup fails before the app runs

        # The OSRM HTTP client was created before its wrapper failed, so it is closed too.
        assert closed == ["osrm", "redis"]

    async def test_startup_failing_before_osrm_closes_only_redis(self, closed, monkeypatch):
        monkeypatch.setattr(main.httpx, "AsyncClient", crash)

        with pytest.raises(AppCrashed):
            async with lifespan(app):
                pass  # pragma: no cover - startup fails before the app runs

        assert closed == ["redis"]

    async def test_failing_close_does_not_skip_the_next_one(self, closed, monkeypatch):
        fail_to_close_osrm(monkeypatch)

        with pytest.raises(CloseFailed):
            async with lifespan(app):
                pass

        assert closed == ["snapshots", "osrm", "redis"]

    async def test_failing_close_keeps_the_error_the_app_stopped_with(self, closed, monkeypatch):
        fail_to_close_osrm(monkeypatch)

        with pytest.raises(CloseFailed) as exc_info:
            async with lifespan(app):
                raise AppCrashed

        assert closed == ["snapshots", "osrm", "redis"]
        # The original error is not lost: it is chained to the one from the close.
        assert isinstance(exc_info.value.__context__, AppCrashed)

    async def test_loop_that_does_not_stop_in_time_is_cancelled(self, closed, monkeypatch, caplog):
        monkeypatch.setattr(config, "DISPATCHER_STOP_TIMEOUT_S", 0.05)

        async def never_stops(self, stop: asyncio.Event) -> None:
            try:
                await asyncio.Event().wait()  # ignores `stop`
            finally:
                closed.append("snapshots")

        monkeypatch.setattr(main.SnapshotBroadcaster, "run", never_stops)

        async with lifespan(app):
            pass

        assert closed == ["snapshots", "osrm", "redis"]
        assert "did not stop in time" in caplog.text
        assert {r.levelno for r in caplog.records if "did not stop" in r.message} == {
            logging.WARNING
        }

    async def test_crashed_loop_is_logged_and_the_shutdown_goes_on(
        self, closed, monkeypatch, caplog
    ):
        async def crashes(self, stop: asyncio.Event) -> None:
            closed.append("snapshots")
            raise RuntimeError("bug in the loop")

        monkeypatch.setattr(main.SnapshotBroadcaster, "run", crashes)

        async with lifespan(app):
            await asyncio.sleep(0)  # let the loop start and crash

        assert closed == ["snapshots", "osrm", "redis"]
        crash_logs = [r for r in caplog.records if "crashed" in r.message]
        assert [r.levelno for r in crash_logs] == [logging.ERROR]


class TestStopTask:
    async def test_cancelled_shutdown_cancels_the_loop_too(self):
        # The loop must not outlive the clients that close right after it.
        loop = asyncio.create_task(asyncio.Event().wait())  # ignores `stop`
        stopping = asyncio.create_task(main._stop_task(loop, asyncio.Event()))
        await asyncio.sleep(0)

        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        await asyncio.gather(loop, return_exceptions=True)

        assert loop.cancelled()
