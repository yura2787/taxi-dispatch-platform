"""App wiring: what the lifespan creates for the request and WebSocket handlers, and
that it closes the clients it created however the app stops."""

import httpx
import pytest

from app import main
from app.clock import Clock
from app.main import app, lifespan
from app.redis_client import create_redis
from app.services.drivers import DriverStore
from app.services.osrm import OsrmClient
from app.ws.manager import ConnectionRegistry

STATE = ("redis", "osrm", "drivers", "driver_connections", "clock")


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
    """Names of the clients the lifespan has closed, in the order it closed them."""
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

    monkeypatch.setattr(main, "create_redis", create_tracked_redis)
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
            assert await app.state.redis.ping()


class TestShutdown:
    async def test_normal_shutdown_closes_every_client(self, closed):
        async with lifespan(app):
            assert closed == []

        # Reverse order of creation: OSRM was created after Redis.
        assert closed == ["osrm", "redis"]

    async def test_app_stopping_with_an_exception_still_closes_every_client(self, closed):
        with pytest.raises(AppCrashed):
            async with lifespan(app):
                raise AppCrashed

        assert closed == ["osrm", "redis"]

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

        assert closed == ["osrm", "redis"]

    async def test_failing_close_keeps_the_error_the_app_stopped_with(self, closed, monkeypatch):
        fail_to_close_osrm(monkeypatch)

        with pytest.raises(CloseFailed) as exc_info:
            async with lifespan(app):
                raise AppCrashed

        assert closed == ["osrm", "redis"]
        # The original error is not lost: it is chained to the one from the close.
        assert isinstance(exc_info.value.__context__, AppCrashed)
