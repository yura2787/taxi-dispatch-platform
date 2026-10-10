"""App wiring: what the lifespan creates for the request and WebSocket handlers."""

from app.clock import Clock
from app.main import app, lifespan
from app.services.drivers import DriverStore
from app.services.osrm import OsrmClient
from app.ws.manager import ConnectionRegistry

STATE = ("redis", "osrm", "drivers", "driver_connections", "clock")


async def test_lifespan_creates_shared_dependencies(monkeypatch):
    # Other tests put their own objects into app.state; keep them out of this one.
    for name in STATE:
        monkeypatch.setattr(app.state, name, None, raising=False)

    async with lifespan(app):
        assert isinstance(app.state.osrm, OsrmClient)
        assert isinstance(app.state.drivers, DriverStore)
        assert isinstance(app.state.driver_connections, ConnectionRegistry)
        assert isinstance(app.state.clock, Clock)
        assert await app.state.redis.ping()
