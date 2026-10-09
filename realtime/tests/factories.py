"""Builders of test data: ``make_<thing>(...)`` with keyword-only arguments and
sensible defaults, so a test spells out only what matters to it."""

from redis.asyncio import Redis

from app import redis_keys


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
