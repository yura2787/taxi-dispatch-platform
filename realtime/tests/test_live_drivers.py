"""Reading the online drivers for the dispatcher map (DriverStore.live_drivers) on a
real Redis: who is on the map and with what, the counters, races with the reaper,
reads in batches, and that the read changes nothing.

Drivers are put into their state by the real scripts (make_live_driver). After every
test the driver invariants (tests/invariants.py) must hold.
"""

import pytest

from app import config, redis_keys
from app.services.drivers import DriverStatus, DriverStore, LiveDriver, LiveDrivers, Outcome
from tests.factories import CENTRE, NOW_MS, make_live_driver
from tests.invariants import assert_driver_invariants

KYIV = (50.4501, 30.5234)


@pytest.fixture
def store(redis) -> DriverStore:
    return DriverStore(redis)


@pytest.fixture(autouse=True)
async def check_invariants(redis):
    yield
    await assert_driver_invariants(redis)


async def dump(redis) -> dict[bytes, tuple[bytes, bool]]:
    """Every key of the DB: its serialized value and whether it expires (DUMP has no
    TTL, and the TTL itself shrinks between two dumps)."""
    return {
        key: (await redis.dump(key), await redis.pttl(key) >= 0) async for key in redis.scan_iter()
    }


async def reap(store: DriverStore, driver_id: int) -> None:
    assert await store.reap(driver_id, cutoff_ms=NOW_MS + 1) is Outcome.REAPED


# ---------------------------------------------------------------------------
# Who is on the map
# ---------------------------------------------------------------------------


class TestDriversOnTheMap:
    async def test_online_driver_in_the_zone_is_shown_connected(self, redis, store):
        await make_live_driver(redis, store, driver_id=1, place=CENTRE, heading=45.0)

        live = await store.live_drivers()

        lat, lon = CENTRE
        assert live.drivers == (
            LiveDriver(
                id=1,
                lat=lat,
                lon=lon,
                heading=45.0,
                status=DriverStatus.AVAILABLE,
                tariff="economy",
                connected=True,
            ),
        )

    async def test_tariff_comes_from_the_state(self, redis, store):
        await make_live_driver(redis, store, driver_id=1, tariff="comfort")

        (driver,) = (await store.live_drivers()).drivers

        assert driver.tariff == "comfort"

    async def test_driver_out_of_the_zone_is_shown_but_not_available(self, redis, store):
        await make_live_driver(redis, store, driver_id=1, place=KYIV)

        live = await store.live_drivers()

        assert [driver.id for driver in live.drivers] == [1]
        assert live.available == 0

    async def test_driver_who_lost_the_connection_is_shown_disconnected(self, redis, store):
        # Dropped, not yet reaped: on the map, greyed out instead of frozen.
        await make_live_driver(redis, store, driver_id=1, connected=False)

        (driver,) = (await store.live_drivers()).drivers

        assert driver.connected is False
        assert driver.status is DriverStatus.AVAILABLE

    async def test_driver_without_a_point_yet_is_not_shown_but_counted(self, redis, store):
        await make_live_driver(redis, store, driver_id=1, place=None)

        live = await store.live_drivers()

        assert live.drivers == ()
        assert live.online == 1

    async def test_point_without_heading_gives_none(self, redis, store):
        await make_live_driver(redis, store, driver_id=1, heading=None)

        (driver,) = (await store.live_drivers()).drivers

        assert driver.heading is None

    @pytest.mark.parametrize("status", [DriverStatus.OFFERED, DriverStatus.ON_TRIP])
    async def test_busy_driver_is_shown_but_not_available(self, redis, store, status):
        # No script sets these before stage 3: written the way it will, out of the GEO
        # sets (no orders during an offer or a trip).
        await make_live_driver(redis, store, driver_id=1)
        await redis.hset(redis_keys.driver_state(1), "status", status)
        await redis.zrem(redis_keys.drivers_geo("economy"), 1)

        live = await store.live_drivers()

        assert [driver.status for driver in live.drivers] == [status]
        assert live.available == 0

    async def test_driver_who_went_offline_is_not_listed(self, redis, store):
        await make_live_driver(redis, store, driver_id=1)
        assert await store.go_offline(1, "conn-1") is Outcome.OK

        assert await store.live_drivers() == LiveDrivers(drivers=(), online=0, available=0)

    async def test_reaped_driver_is_not_listed(self, redis, store):
        await make_live_driver(redis, store, driver_id=1, connected=False)
        await reap(store, 1)

        assert await store.live_drivers() == LiveDrivers(drivers=(), online=0, available=0)

    async def test_drivers_are_ordered_by_id(self, redis, store):
        for driver_id in (3, 1, 2):
            await make_live_driver(redis, store, driver_id=driver_id)

        live = await store.live_drivers()

        assert [driver.id for driver in live.drivers] == [1, 2, 3]

    async def test_empty_city_has_no_drivers_and_zero_counters(self, store):
        assert await store.live_drivers() == LiveDrivers(drivers=(), online=0, available=0)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


class TestCounters:
    async def test_counters_match_count_drivers(self, redis, store):
        await make_live_driver(redis, store, driver_id=1)
        await make_live_driver(redis, store, driver_id=2, tariff="comfort")
        await make_live_driver(redis, store, driver_id=3, place=KYIV)
        await make_live_driver(redis, store, driver_id=4, connected=False)
        await make_live_driver(redis, store, driver_id=5, place=None)

        live = await store.live_drivers()
        counts = await store.count_drivers()

        assert (live.online, live.available) == (counts.online, counts.available) == (5, 2)


# ---------------------------------------------------------------------------
# Races and batches
# ---------------------------------------------------------------------------


class TestRaces:
    @pytest.mark.parametrize("state_expired", [False, True], ids=["reaped", "state expired"])
    async def test_driver_gone_between_listing_and_reading_is_skipped(
        self, redis, store, monkeypatch, state_expired
    ):
        await make_live_driver(redis, store, driver_id=1, connected=False)
        await make_live_driver(redis, store, driver_id=2)
        listing = await redis.zrange(redis_keys.drivers_last_seen(), 0, -1)
        # After the listing, the reaper takes driver 1 offline (and the state may expire).
        await reap(store, 1)
        if state_expired:
            await redis.delete(redis_keys.driver_state(1))

        async def stale_listing(name, start, end):
            # Fails if the listing ever comes from somewhere else.
            assert (name, start, end) == (redis_keys.drivers_last_seen(), 0, -1)
            return listing

        monkeypatch.setattr(redis, "zrange", stale_listing)
        live = await store.live_drivers()

        assert [driver.id for driver in live.drivers] == [2]
        # The count of the listing, not recounted.
        assert live.online == 2

    @pytest.mark.parametrize("count", [2, 4, 5], ids=["one batch", "two full", "partial last"])
    async def test_drivers_are_read_in_batches(self, redis, store, monkeypatch, count):
        monkeypatch.setattr(config, "SNAPSHOT_READ_BATCH", 2)
        # Driver 1 has no point: skipped inside the first batch, so a reply paired with
        # the wrong driver would show.
        await make_live_driver(redis, store, driver_id=1, place=None)
        for driver_id in range(2, count + 1):
            await make_live_driver(redis, store, driver_id=driver_id)

        live = await store.live_drivers()

        assert [driver.id for driver in live.drivers] == list(range(2, count + 1))
        assert live.online == count
        # The GEO counts are read once, not once per batch.
        assert live.available == count - 1


class TestReadOnly:
    async def test_reading_changes_nothing_in_redis(self, redis, store):
        await make_live_driver(redis, store, driver_id=1)
        await make_live_driver(redis, store, driver_id=2, place=KYIV)
        await make_live_driver(redis, store, driver_id=3, connected=False)
        await make_live_driver(redis, store, driver_id=4, place=None)
        before = await dump(redis)

        await store.live_drivers()

        assert await dump(redis) == before
