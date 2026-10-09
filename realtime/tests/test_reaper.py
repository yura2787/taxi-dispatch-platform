"""Reaper of the dispatcher process on a real Redis: which drivers a pass takes
offline, batching, the drivers_online gauge and the loop that survives failures.

After every test the driver invariants (tests/invariants.py) must hold.
"""

import asyncio

import pytest
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError

from app import config
from app.services.drivers import DriverCounts, DriverStatus, DriverStore, LocationUpdate
from app.services.locations import AcceptedPoint
from app.services.reaper import Reaper, run_reaper
from tests.factories import make_driver_profile
from tests.invariants import assert_driver_invariants

CENTRE = (48.2921, 25.9358)
KYIV = (50.4501, 30.5234)


@pytest.fixture
def store(redis) -> DriverStore:
    return DriverStore(redis)


@pytest.fixture
def reaper(store, clock) -> Reaper:
    return Reaper(store, clock)


@pytest.fixture(autouse=True)
async def check_invariants(redis):
    yield
    await assert_driver_invariants(redis)


async def make_online_driver(redis, store, clock, driver_id: int, *, in_zone: bool = True) -> None:
    """A driver who went online and sent a point now (server time of `clock`)."""
    await make_driver_profile(redis, driver_id=driver_id)
    conn_id = f"conn-{driver_id}"
    await store.connect(driver_id, conn_id)
    await store.go_online(driver_id, conn_id, clock.now_ms())
    lat, lon = CENTRE if in_zone else KYIV
    now_ms = clock.now_ms()
    point = AcceptedPoint(lat=lat, lon=lon, ts=now_ms, server_ms=now_ms)
    update = LocationUpdate(point=point, heading=None, speed=None, in_zone=in_zone)
    await store.update_location(driver_id, conn_id, update)


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------


class TestRunOnce:
    async def test_takes_the_silent_driver_offline_and_keeps_the_active_one(
        self, redis, store, clock, reaper
    ):
        await make_online_driver(redis, store, clock, 1)
        clock.advance(20)
        await make_online_driver(redis, store, clock, 2)
        clock.advance(15)  # driver 1 silent for 35 s, driver 2 for 15 s

        reaped = await reaper.run_once()

        assert reaped == 1
        assert (await store.get_state(1)).status is DriverStatus.OFFLINE
        assert (await store.get_state(2)).status is DriverStatus.AVAILABLE

    async def test_driver_silent_for_exactly_the_limit_is_kept(self, redis, store, clock, reaper):
        await make_online_driver(redis, store, clock, 1)
        clock.advance(config.DRIVER_SILENCE_S)

        assert await reaper.run_once() == 0
        assert (await store.get_state(1)).status is DriverStatus.AVAILABLE

    async def test_pass_with_nobody_online_reaps_nobody(self, reaper):
        assert await reaper.run_once() == 0

    async def test_more_silent_drivers_than_a_batch_are_all_reaped_in_one_pass(
        self, redis, store, clock, reaper, monkeypatch
    ):
        monkeypatch.setattr(config, "REAPER_BATCH", 2)
        for driver_id in range(1, 6):
            await make_online_driver(redis, store, clock, driver_id)
        clock.advance(config.DRIVER_SILENCE_S + 1)

        reaped = await reaper.run_once()

        assert reaped == 5
        assert await store.count_drivers() == DriverCounts(online=0, available=0)

    async def test_reaped_drivers_are_counted_and_logged(self, redis, store, clock, reaper, caplog):
        await make_online_driver(redis, store, clock, 7)
        clock.advance(config.DRIVER_SILENCE_S + 1)
        reaped_before = sample("reaper_reaped_total")
        passes_before = sample("reaper_iteration_seconds_count")

        await reaper.run_once()

        assert sample("reaper_reaped_total") == reaped_before + 1
        assert sample("reaper_iteration_seconds_count") == passes_before + 1
        assert "Driver 7 reaped" in caplog.text

    async def test_drivers_online_counts_online_and_offerable_drivers(
        self, redis, store, clock, reaper
    ):
        await make_online_driver(redis, store, clock, 1)
        await make_online_driver(redis, store, clock, 2, in_zone=False)
        await make_driver_profile(redis, driver_id=3)
        await store.connect(3, "conn-3")  # connected, but offline

        await reaper.run_once()

        assert sample("drivers_online", status="online") == 2
        assert sample("drivers_online", status="available") == 1

    async def test_drivers_online_reflects_the_pass(self, redis, store, clock, reaper):
        await make_online_driver(redis, store, clock, 1)
        clock.advance(config.DRIVER_SILENCE_S + 1)

        await reaper.run_once()

        assert sample("drivers_online", status="online") == 0
        assert sample("drivers_online", status="available") == 0


class TestSilentDrivers:
    async def test_are_listed_oldest_first_up_to_the_limit(self, redis, store, clock):
        for driver_id in (3, 1, 2):
            await make_online_driver(redis, store, clock, driver_id)
            clock.advance(1)

        silent = await store.silent_drivers(clock.now_ms(), limit=2)

        assert silent == [3, 1]

    async def test_driver_seen_exactly_at_the_cutoff_is_not_silent(self, redis, store, clock):
        await make_online_driver(redis, store, clock, 1)

        assert await store.silent_drivers(clock.now_ms(), limit=10) == []


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class ScriptedReaper:
    """Fails the passes listed in `fail_on` and sets `stop` on pass `stop_on`."""

    def __init__(self, stop: asyncio.Event, *, stop_on: int, fail_on: tuple[int, ...] = ()):
        self.stop = stop
        self.stop_on = stop_on
        self.fail_on = fail_on
        self.passes = 0

    async def run_once(self) -> int:
        self.passes += 1
        if self.passes == self.stop_on:
            self.stop.set()
        if self.passes in self.fail_on:
            raise RedisConnectionError("Redis is restarting")
        return 0


class TestRunReaper:
    async def test_failed_pass_is_logged_and_the_loop_goes_on(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "REAPER_INTERVAL_S", 0)
        stop = asyncio.Event()
        reaper = ScriptedReaper(stop, stop_on=3, fail_on=(1, 2))

        await run_reaper(reaper, stop)

        assert reaper.passes == 3
        assert caplog.text.count("Reaper pass failed") == 2
        assert all(record.levelname == "WARNING" for record in caplog.records)

    async def test_stop_ends_the_loop_without_waiting_for_the_interval(self):
        # The interval stays at its real 10 s: the loop must not sleep through it.
        stop = asyncio.Event()
        reaper = ScriptedReaper(stop, stop_on=1)

        async with asyncio.timeout(1):
            await run_reaper(reaper, stop)

        assert reaper.passes == 1

    async def test_loop_does_not_start_once_stopped(self):
        stop = asyncio.Event()
        stop.set()
        reaper = ScriptedReaper(stop, stop_on=1)

        await run_reaper(reaper, stop)

        assert reaper.passes == 0
