"""Builders of test data: ``make_<thing>(...)`` with keyword-only arguments and
sensible defaults, so a test spells out only what matters to it."""

from redis.asyncio import Redis

from app import redis_keys
from app.services.drivers import DriverStore, LocationUpdate, Outcome
from app.services.locations import AcceptedPoint, in_service_area

# Central Square, Chernivtsi.
CENTRE = (48.2921, 25.9358)
# 2026-01-01 00:00 UTC, the start of FakeClock.
NOW_MS = 1_767_225_600_000


async def make_driver_profile(
    redis: Redis,
    *,
    driver_id: int = 1,
    eligible: bool = True,
    tariff: str = "economy",
    name: str = "Taras Melnyk",
    car: str = "Skoda Octavia",
    color: str = "white",
    plate: str = "CE 1234 AA",
    rating: float = 4.9,
) -> int:
    """Write a driver profile the way Django will (stage 2); returns the driver id."""
    await redis.hset(
        redis_keys.driver_profile(driver_id),
        mapping={
            "eligible": int(eligible),
            "tariff": tariff,
            "name": name,
            "car": car,
            "color": color,
            "plate": plate,
            "rating": rating,
        },
    )
    return driver_id


async def make_live_driver(
    redis: Redis,
    store: DriverStore,
    *,
    driver_id: int = 1,
    place: tuple[float, float] | None = CENTRE,
    tariff: str = "economy",
    heading: float | None = 90.0,
    connected: bool = True,
    now_ms: int = NOW_MS,
) -> int:
    """An online driver, put into that state by the real scripts (connect, go_online,
    a point, and a disconnect if not connected), so the data is always what the
    gateway writes. place=None: online without a point yet. Returns the driver id."""
    await make_driver_profile(redis, driver_id=driver_id, tariff=tariff)
    conn_id = f"conn-{driver_id}"
    await store.connect(driver_id, conn_id)
    assert await store.go_online(driver_id, conn_id, now_ms) is Outcome.OK
    if place is not None:
        lat, lon = place
        point = AcceptedPoint(lat=lat, lon=lon, ts=now_ms, server_ms=now_ms)
        update = LocationUpdate(
            point=point, heading=heading, speed=None, in_zone=in_service_area(point)
        )
        assert await store.update_location(driver_id, conn_id, update) is Outcome.OK
    if not connected:
        assert await store.disconnect(driver_id, conn_id) is Outcome.OK
    return driver_id
