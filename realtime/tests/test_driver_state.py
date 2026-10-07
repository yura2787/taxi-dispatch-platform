"""Driver state Lua scripts on a real Redis: connect, going online and offline,
locations and the GEO sets, disconnects, the reaper and the races between them.

After every test the driver invariants (tests/invariants.py) must hold.
"""

import pytest

from app import config, redis_keys
from app.services.drivers import (
    DriverState,
    DriverStatus,
    DriverStore,
    LocationUpdate,
    Outcome,
    Position,
)
from app.services.locations import AcceptedPoint
from tests.factories import make_driver_profile
from tests.invariants import assert_driver_invariants

DRIVER = 1
CONN = "conn-1"
OTHER_CONN = "conn-2"

CENTRE = (48.2921, 25.9358)
KYIV = (50.4501, 30.5234)


@pytest.fixture
def store(redis) -> DriverStore:
    return DriverStore(redis)


@pytest.fixture(autouse=True)
async def check_invariants(redis):
    yield
    await assert_driver_invariants(redis)


@pytest.fixture
async def connected(redis, store):
    """A driver with a profile, connected and offline."""
    await make_driver_profile(redis, driver_id=DRIVER)
    await store.connect(DRIVER, CONN)


@pytest.fixture
async def online(connected, store, clock):
    """A connected driver who went online and sent a point in the centre."""
    assert await store.go_online(DRIVER, CONN, clock.now_ms()) is Outcome.OK
    assert await store.update_location(DRIVER, CONN, update(clock)) is Outcome.OK


def update(clock, place=CENTRE, *, in_zone=True, heading=90.0, speed=12.5) -> LocationUpdate:
    lat, lon = place
    now_ms = clock.now_ms()
    return LocationUpdate(
        point=AcceptedPoint(lat=lat, lon=lon, ts=now_ms - 500, server_ms=now_ms),
        heading=heading,
        speed=speed,
        in_zone=in_zone,
    )


async def drivers_near(redis, place=CENTRE, tariff="economy") -> list[int]:
    lat, lon = place
    found = await redis.geosearch(
        redis_keys.drivers_geo(tariff), longitude=lon, latitude=lat, radius=100, unit="m"
    )
    return [int(member) for member in found]


async def in_any_geo(redis, driver_id=DRIVER) -> bool:
    for tariff in config.TARIFFS:
        if await redis.zscore(redis_keys.drivers_geo(tariff), driver_id) is not None:
            return True
    return False


async def last_seen(redis, driver_id=DRIVER) -> float | None:
    return await redis.zscore(redis_keys.drivers_last_seen(), driver_id)


async def state_ttl(redis, driver_id=DRIVER) -> int:
    return await redis.ttl(redis_keys.driver_state(driver_id))


# ---------------------------------------------------------------------------
# Profile and state reading
# ---------------------------------------------------------------------------


class TestReading:
    async def test_missing_profile_is_none(self, store):
        assert await store.get_profile(DRIVER) is None

    async def test_profile_is_parsed(self, redis, store):
        await make_driver_profile(redis, driver_id=DRIVER, tariff="comfort", rating=4.7)

        profile = await store.get_profile(DRIVER)

        assert profile.eligible is True
        assert profile.tariff == "comfort"
        assert profile.rating == 4.7
        assert profile.may_go_online

    @pytest.mark.parametrize(
        ("eligible", "tariff"),
        [(False, "economy"), (True, "premium")],
        ids=["not-eligible", "unknown-tariff"],
    )
    async def test_profile_may_not_go_online(self, redis, store, eligible, tariff):
        await make_driver_profile(redis, driver_id=DRIVER, eligible=eligible, tariff=tariff)

        assert not (await store.get_profile(DRIVER)).may_go_online

    async def test_missing_state_is_an_offline_driver_without_position(self, store):
        assert await store.get_state(DRIVER) == DriverState(
            status=DriverStatus.OFFLINE, tariff=None, in_zone=False, position=None, conn_id=None
        )

    async def test_state_of_a_driver_online_with_a_point(self, store, online, clock):
        state = await store.get_state(DRIVER)

        assert state.status is DriverStatus.AVAILABLE
        assert state.tariff == "economy"
        assert state.in_zone is True
        assert state.conn_id == CONN
        assert state.position == Position(
            lat=CENTRE[0],
            lon=CENTRE[1],
            heading=90.0,
            speed=12.5,
            ts=clock.now_ms() - 500,
            updated_at=clock.now_ms(),
        )
        assert state.position.as_accepted_point() == update(clock).point


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_new_driver_is_offline_with_ttl_and_this_connection(self, redis, store):
        state = await store.connect(DRIVER, CONN)

        assert state.status is DriverStatus.OFFLINE
        assert state.conn_id == CONN
        assert 0 < await state_ttl(redis) <= config.OFFLINE_STATE_TTL_S

    async def test_online_driver_keeps_status_and_position_but_changes_connection(
        self, redis, store, online
    ):
        state = await store.connect(DRIVER, OTHER_CONN)

        assert state.status is DriverStatus.AVAILABLE
        assert state.position is not None
        assert state.conn_id == OTHER_CONN
        assert await state_ttl(redis) == -1  # no TTL while online
        assert await drivers_near(redis) == [DRIVER]


# ---------------------------------------------------------------------------
# go_online
# ---------------------------------------------------------------------------


class TestGoOnline:
    async def test_makes_driver_available_with_profile_tariff_and_no_ttl(self, redis, store, clock):
        await make_driver_profile(redis, driver_id=DRIVER, tariff="comfort")
        await store.connect(DRIVER, CONN)

        outcome = await store.go_online(DRIVER, CONN, clock.now_ms())

        assert outcome is Outcome.OK
        state = await store.get_state(DRIVER)
        assert state.status is DriverStatus.AVAILABLE
        assert state.tariff == "comfort"
        assert await last_seen(redis) == clock.now_ms()
        assert await state_ttl(redis) == -1

    async def test_does_not_put_driver_into_geo_before_a_point(
        self, redis, store, connected, clock
    ):
        await store.go_online(DRIVER, CONN, clock.now_ms())

        assert not await in_any_geo(redis)

    @pytest.mark.parametrize(
        ("eligible", "tariff"),
        [(False, "economy"), (True, "premium")],
        ids=["not-eligible", "unknown-tariff"],
    )
    async def test_ineligible_driver_stays_offline(self, redis, store, clock, eligible, tariff):
        await make_driver_profile(redis, driver_id=DRIVER, eligible=eligible, tariff=tariff)
        await store.connect(DRIVER, CONN)

        outcome = await store.go_online(DRIVER, CONN, clock.now_ms())

        assert outcome is Outcome.NOT_ELIGIBLE
        assert (await store.get_state(DRIVER)).status is DriverStatus.OFFLINE

    async def test_driver_without_profile_is_not_eligible(self, store, clock):
        await store.connect(DRIVER, CONN)

        assert await store.go_online(DRIVER, CONN, clock.now_ms()) is Outcome.NOT_ELIGIBLE

    async def test_repeated_call_changes_nothing(self, redis, store, online, clock):
        before = await store.get_state(DRIVER)
        first_seen = await last_seen(redis)
        clock.advance(5)

        outcome = await store.go_online(DRIVER, CONN, clock.now_ms())

        assert outcome is Outcome.OK
        assert await store.get_state(DRIVER) == before
        assert await last_seen(redis) == first_seen
        assert await drivers_near(redis) == [DRIVER]

    async def test_foreign_connection_is_stale(self, store, connected, clock):
        outcome = await store.go_online(DRIVER, OTHER_CONN, clock.now_ms())

        assert outcome is Outcome.STALE_CONNECTION
        assert (await store.get_state(DRIVER)).status is DriverStatus.OFFLINE

    @pytest.mark.parametrize("status", ["offered", "on_trip"])
    async def test_busy_driver_is_refused(self, redis, store, online, clock, status):
        # Stage 3 sets these statuses; until then they are written by hand.
        await redis.hset(redis_keys.driver_state(DRIVER), "status", status)
        await redis.zrem(redis_keys.drivers_geo("economy"), DRIVER)

        assert await store.go_online(DRIVER, CONN, clock.now_ms()) is Outcome.BUSY


# ---------------------------------------------------------------------------
# location
# ---------------------------------------------------------------------------


class TestLocation:
    async def test_available_driver_in_zone_is_found_in_geo_of_their_tariff(
        self, redis, store, online
    ):
        assert await drivers_near(redis, tariff="economy") == [DRIVER]
        assert await drivers_near(redis, tariff="comfort") == []

    async def test_point_out_of_zone_updates_position_and_last_seen_but_leaves_geo(
        self, redis, store, online, clock
    ):
        clock.advance(3)

        outcome = await store.update_location(DRIVER, CONN, update(clock, KYIV, in_zone=False))

        assert outcome is Outcome.OK
        state = await store.get_state(DRIVER)
        assert (state.position.lat, state.position.lon) == KYIV
        assert state.in_zone is False
        assert state.status is DriverStatus.AVAILABLE
        assert await last_seen(redis) == clock.now_ms()
        assert not await in_any_geo(redis)

    async def test_back_in_zone_returns_driver_to_geo(self, redis, store, online, clock):
        clock.advance(3)
        await store.update_location(DRIVER, CONN, update(clock, KYIV, in_zone=False))
        clock.advance(3)

        await store.update_location(DRIVER, CONN, update(clock))

        assert await drivers_near(redis) == [DRIVER]

    async def test_missing_heading_and_speed_are_removed_from_state(self, store, online, clock):
        clock.advance(3)

        await store.update_location(DRIVER, CONN, update(clock, heading=None, speed=None))

        position = (await store.get_state(DRIVER)).position
        assert position.heading is None
        assert position.speed is None

    async def test_offline_driver_is_not_online(self, redis, store, connected, clock):
        outcome = await store.update_location(DRIVER, CONN, update(clock))

        assert outcome is Outcome.NOT_ONLINE
        assert (await store.get_state(DRIVER)).position is None
        assert await last_seen(redis) is None

    async def test_foreign_connection_is_stale(self, store, online, clock):
        clock.advance(3)

        outcome = await store.update_location(
            DRIVER, OTHER_CONN, update(clock, KYIV, in_zone=False)
        )

        assert outcome is Outcome.STALE_CONNECTION
        assert (await store.get_state(DRIVER)).in_zone is True

    async def test_busy_driver_is_tracked_but_not_offered(self, redis, store, online, clock):
        await redis.hset(redis_keys.driver_state(DRIVER), "status", "on_trip")
        clock.advance(3)

        outcome = await store.update_location(DRIVER, CONN, update(clock))

        assert outcome is Outcome.OK
        assert await last_seen(redis) == clock.now_ms()
        assert not await in_any_geo(redis)

    async def test_driver_is_never_in_two_geo_sets(self, redis, store, online, clock):
        # The tariff changed while online (stage 2: from Django); the old set is left.
        await redis.hset(redis_keys.driver_state(DRIVER), "tariff", "comfort")
        clock.advance(3)

        await store.update_location(DRIVER, CONN, update(clock))

        assert await drivers_near(redis, tariff="comfort") == [DRIVER]
        assert await drivers_near(redis, tariff="economy") == []


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


class TestDisconnect:
    async def test_own_connection_leaves_geo_but_keeps_status_and_last_seen(
        self, redis, store, online
    ):
        seen = await last_seen(redis)

        outcome = await store.disconnect(DRIVER, CONN)

        assert outcome is Outcome.OK
        state = await store.get_state(DRIVER)
        assert state.status is DriverStatus.AVAILABLE
        assert state.conn_id is None
        assert await last_seen(redis) == seen
        assert not await in_any_geo(redis)

    async def test_reconnect_and_a_point_return_driver_to_geo(self, redis, store, online, clock):
        await store.disconnect(DRIVER, CONN)
        clock.advance(10)

        await store.connect(DRIVER, OTHER_CONN)
        await store.update_location(DRIVER, OTHER_CONN, update(clock))

        assert await drivers_near(redis) == [DRIVER]

    async def test_old_connection_closed_after_a_takeover_changes_nothing(
        self, redis, store, online
    ):
        # The 4409 race: the new connection is bound first, then the old one closes.
        await store.connect(DRIVER, OTHER_CONN)

        outcome = await store.disconnect(DRIVER, CONN)

        assert outcome is Outcome.NOT_OWNER
        assert (await store.get_state(DRIVER)).conn_id == OTHER_CONN
        assert await drivers_near(redis) == [DRIVER]


# ---------------------------------------------------------------------------
# reap
# ---------------------------------------------------------------------------


class TestReap:
    def cutoff(self, clock) -> int:
        return clock.now_ms() - config.DRIVER_SILENCE_S * 1000

    async def test_silent_driver_goes_offline_and_leaves_every_set(
        self, redis, store, online, clock
    ):
        clock.advance(config.DRIVER_SILENCE_S + 1)

        outcome = await store.reap(DRIVER, self.cutoff(clock))

        assert outcome is Outcome.REAPED
        assert (await store.get_state(DRIVER)).status is DriverStatus.OFFLINE
        assert await last_seen(redis) is None
        assert not await in_any_geo(redis)
        assert 0 < await state_ttl(redis) <= config.OFFLINE_STATE_TTL_S

    async def test_driver_silent_for_exactly_the_limit_is_alive(self, store, online, clock):
        clock.advance(config.DRIVER_SILENCE_S)

        assert await store.reap(DRIVER, self.cutoff(clock)) is Outcome.ALIVE

    async def test_point_between_listing_and_reaping_keeps_the_driver(
        self, redis, store, online, clock
    ):
        # The reaper listed the driver as silent...
        clock.advance(config.DRIVER_SILENCE_S + 1)
        cutoff = self.cutoff(clock)
        # ...then a point arrived before the script ran for them.
        await store.update_location(DRIVER, CONN, update(clock))

        outcome = await store.reap(DRIVER, cutoff)

        assert outcome is Outcome.ALIVE
        assert (await store.get_state(DRIVER)).status is DriverStatus.AVAILABLE
        assert await drivers_near(redis) == [DRIVER]

    async def test_reaping_twice_changes_nothing_the_second_time(self, store, online, clock):
        clock.advance(config.DRIVER_SILENCE_S + 1)
        await store.reap(DRIVER, self.cutoff(clock))
        after_first = await store.get_state(DRIVER)

        outcome = await store.reap(DRIVER, self.cutoff(clock))

        assert outcome is Outcome.GONE
        assert await store.get_state(DRIVER) == after_first

    async def test_reaped_driver_still_connected_gets_not_online_for_a_point(
        self, store, online, clock
    ):
        clock.advance(config.DRIVER_SILENCE_S + 1)
        await store.reap(DRIVER, self.cutoff(clock))

        assert await store.update_location(DRIVER, CONN, update(clock)) is Outcome.NOT_ONLINE


# ---------------------------------------------------------------------------
# go_offline
# ---------------------------------------------------------------------------


class TestGoOffline:
    async def test_leaves_every_set_and_sets_ttl(self, redis, store, online):
        outcome = await store.go_offline(DRIVER, CONN)

        assert outcome is Outcome.OK
        state = await store.get_state(DRIVER)
        assert state.status is DriverStatus.OFFLINE
        assert state.position is not None  # kept for a quick return
        assert await last_seen(redis) is None
        assert not await in_any_geo(redis)
        assert 0 < await state_ttl(redis) <= config.OFFLINE_STATE_TTL_S

    async def test_profile_has_no_ttl(self, redis, store, online):
        await store.go_offline(DRIVER, CONN)

        assert await redis.ttl(redis_keys.driver_profile(DRIVER)) == -1

    async def test_offline_driver_is_not_online(self, store, connected):
        assert await store.go_offline(DRIVER, CONN) is Outcome.NOT_ONLINE

    async def test_foreign_connection_is_stale(self, store, online):
        assert await store.go_offline(DRIVER, OTHER_CONN) is Outcome.STALE_CONNECTION
        assert (await store.get_state(DRIVER)).status is DriverStatus.AVAILABLE

    @pytest.mark.parametrize("status", ["offered", "on_trip"])
    async def test_busy_driver_is_refused(self, redis, store, online, status):
        await redis.hset(redis_keys.driver_state(DRIVER), "status", status)
        await redis.zrem(redis_keys.drivers_geo("economy"), DRIVER)

        assert await store.go_offline(DRIVER, CONN) is Outcome.BUSY

    async def test_go_online_again_removes_the_ttl(self, redis, store, online, clock):
        await store.go_offline(DRIVER, CONN)

        await store.go_online(DRIVER, CONN, clock.now_ms())

        assert await state_ttl(redis) == -1


# ---------------------------------------------------------------------------
# The invariant check itself
# ---------------------------------------------------------------------------


class TestInvariantCheck:
    """The check must catch violations, or every test above would pass vacuously."""

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("status", "offline", "has status offline"),
            ("in_zone", "0", "out of the zone"),
            ("tariff", "comfort", "has tariff comfort"),
        ],
    )
    async def test_detects_a_driver_in_geo_who_cannot_get_an_offer(
        self, redis, store, online, field, value, message
    ):
        await redis.hset(redis_keys.driver_state(DRIVER), field, value)

        with pytest.raises(AssertionError, match=message):
            await assert_driver_invariants(redis)

        await redis.flushdb()

    async def test_detects_a_driver_in_geo_without_connection(self, redis, store, online):
        await redis.hdel(redis_keys.driver_state(DRIVER), "conn_id")

        with pytest.raises(AssertionError, match="no connection"):
            await assert_driver_invariants(redis)

        await redis.flushdb()

    async def test_detects_a_driver_in_geo_but_not_online(self, redis, store, online):
        await redis.zrem(redis_keys.drivers_last_seen(), DRIVER)

        with pytest.raises(AssertionError, match="not in drivers:last_seen"):
            await assert_driver_invariants(redis)

        await redis.flushdb()

    async def test_detects_a_driver_in_two_geo_sets(self, redis, store, online):
        await redis.geoadd(redis_keys.drivers_geo("comfort"), (CENTRE[1], CENTRE[0], DRIVER))

        with pytest.raises(AssertionError, match="two GEO sets"):
            await assert_driver_invariants(redis)

        await redis.flushdb()

    async def test_detects_an_offline_driver_in_last_seen(self, redis, store, connected):
        await redis.zadd(redis_keys.drivers_last_seen(), {DRIVER: 1})

        with pytest.raises(AssertionError, match="offline driver 1 is in drivers:last_seen"):
            await assert_driver_invariants(redis)

        await redis.flushdb()

    async def test_detects_an_offline_state_without_ttl(self, redis, store, connected):
        await redis.persist(redis_keys.driver_state(DRIVER))

        with pytest.raises(AssertionError, match="has no TTL"):
            await assert_driver_invariants(redis)

        await redis.flushdb()
