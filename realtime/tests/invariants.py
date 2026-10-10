"""Invariants of the driver state in Redis that must hold after any sequence of
operations; tests that change driver state check them at the end."""

from redis.asyncio import Redis

from app import config, redis_keys


async def assert_driver_invariants(redis: Redis) -> None:
    last_seen = {
        int(member): score
        for member, score in await redis.zrange(
            redis_keys.drivers_last_seen(), 0, -1, withscores=True
        )
    }

    geo_tariff: dict[int, str] = {}
    for tariff in config.TARIFFS:
        for member in await redis.zrange(redis_keys.drivers_geo(tariff), 0, -1):
            driver_id = int(member)
            # 4. In at most one GEO set.
            assert driver_id not in geo_tariff, f"driver {driver_id} is in two GEO sets"
            geo_tariff[driver_id] = tariff

    # 1. In a GEO set => an order can be offered: available, in the zone, connected,
    # with the tariff of that set, and online.
    for driver_id, tariff in geo_tariff.items():
        state = await _state(redis, driver_id)
        where = f"driver {driver_id} in drivers:geo:{tariff}"
        assert state.get("status") == "available", f"{where} has status {state.get('status')}"
        assert state.get("in_zone") == "1", f"{where} is out of the zone"
        assert state.get("conn_id"), f"{where} has no connection"
        assert state.get("tariff") == tariff, f"{where} has tariff {state.get('tariff')}"
        assert driver_id in last_seen, f"{where} is not in drivers:last_seen"

    # 2. In last_seen => not offline.
    for driver_id in last_seen:
        status = (await _state(redis, driver_id)).get("status", "offline")
        assert status != "offline", f"offline driver {driver_id} is in drivers:last_seen"

    # 3. Offline (or no status) => the state expires.
    async for key in redis.scan_iter(match=redis_keys.driver_state("*")):
        status = await redis.hget(key, "status")
        if status in (None, b"offline"):
            assert await redis.ttl(key) > 0, f"offline {key.decode()} has no TTL"


async def _state(redis: Redis, driver_id: int) -> dict[str, str]:
    raw = await redis.hgetall(redis_keys.driver_state(driver_id))
    return {key.decode(): value.decode() for key, value in raw.items()}
