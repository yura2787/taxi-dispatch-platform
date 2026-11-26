"""Snapshots for the dispatcher map pages (app/ws/broadcaster.py): the latest-wins
slot of one page, and the broadcaster that builds one snapshot per tick for all of
them (subscriptions and their limit, failures, metrics).

The broadcaster gets a stand-in DriverStore: what it reads from Redis is covered by
test_live_drivers.py. No timers: time is FakeClock, and a waiting task is given a
chance to run with yields to the event loop (asyncio.sleep(0)), never a real delay.
"""

import asyncio
import json
import logging

import pytest
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError

from app import config
from app.services.drivers import DriverStatus, LiveDriver, LiveDrivers
from app.services.snapshot import build_dispatcher_snapshot
from app.ws.broadcaster import SnapshotBroadcaster, SnapshotSlot, TooManySubscribers


def sample(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


def skipped() -> float:
    return sample("dispatcher_snapshot_skipped_total")


async def let_tasks_run() -> None:
    """Yield to the event loop a few times, so started tasks reach their next await."""
    for _ in range(3):
        await asyncio.sleep(0)


async def cancel(*tasks: asyncio.Task) -> None:
    """Cancel the tasks and wait until they are gone, so none outlives the test."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class TestSnapshotSlot:
    async def test_wait_returns_the_snapshot_put(self):
        slot = SnapshotSlot()
        slot.put("first")

        assert await slot.wait() == "first"

    async def test_wait_blocks_until_a_snapshot_is_put(self):
        slot = SnapshotSlot()
        waiting = asyncio.create_task(slot.wait())
        await let_tasks_run()
        assert not waiting.done()

        slot.put("first")

        assert await waiting == "first"

    async def test_latest_snapshot_wins_and_the_replaced_ones_are_counted(self):
        slot = SnapshotSlot()
        before = skipped()

        for text in ("first", "second", "third"):
            slot.put(text)

        assert await slot.wait() == "third"
        assert skipped() == before + 2

    async def test_taken_snapshot_is_not_returned_again(self):
        slot = SnapshotSlot()
        slot.put("first")
        await slot.wait()

        waiting = asyncio.create_task(slot.wait())
        try:
            await let_tasks_run()

            assert not waiting.done()
        finally:
            await cancel(waiting)

    async def test_snapshot_put_after_the_previous_one_was_taken_is_not_skipped(self):
        slot = SnapshotSlot()
        slot.put("first")
        await slot.wait()
        before = skipped()

        slot.put("second")

        assert await slot.wait() == "second"
        assert skipped() == before

    async def test_cancelled_wait_leaves_the_snapshot_for_the_next_one(self):
        # The session cancels its wait when it leaves; nothing may get lost before.
        slot = SnapshotSlot()
        waiting = asyncio.create_task(slot.wait())
        await let_tasks_run()
        slot.put("first")
        await cancel(waiting)

        assert await slot.wait() == "first"

    async def test_waiting_session_gets_the_latest_of_two_quick_snapshots(self):
        slot = SnapshotSlot()
        waiting = asyncio.create_task(slot.wait())
        await let_tasks_run()
        before = skipped()

        # Both arrive before the woken session gets to run.
        slot.put("first")
        slot.put("second")

        assert await waiting == "second"
        assert skipped() == before + 1

    async def test_cancelled_wait_on_an_empty_slot_leaves_it_usable(self):
        slot = SnapshotSlot()
        waiting = asyncio.create_task(slot.wait())
        await let_tasks_run()
        await cancel(waiting)

        slot.put("first")

        assert await slot.wait() == "first"

    async def test_second_waiter_woken_with_nothing_left_waits_again(self):
        slot = SnapshotSlot()
        first = asyncio.create_task(slot.wait())
        second = asyncio.create_task(slot.wait())
        try:
            await let_tasks_run()
            slot.put("only")
            await let_tasks_run()

            # One of them took it; the other still waits instead of returning None.
            assert {first.done(), second.done()} == {True, False}
        finally:
            await cancel(first, second)


# ---------------------------------------------------------------------------
# The broadcaster
# ---------------------------------------------------------------------------

CENTRE_DRIVER = LiveDriver(
    id=1,
    lat=48.2921,
    lon=25.9358,
    heading=90.0,
    status=DriverStatus.AVAILABLE,
    tariff="economy",
    connected=True,
)
CITY = LiveDrivers(drivers=(CENTRE_DRIVER,), online=1, available=1)


class StubStore:
    """Stands in for DriverStore: returns `live`, or raises while `failing`."""

    def __init__(self, live: LiveDrivers = CITY) -> None:
        self.live = live
        self.failing = False
        self.reads = 0
        # Set it to hold a read until the test lets it finish (release.set()).
        self.release: asyncio.Event | None = None

    async def live_drivers(self) -> LiveDrivers:
        self.reads += 1
        if self.release is not None:
            await self.release.wait()
        if self.failing:
            raise RedisConnectionError("Redis is down")
        return self.live


@pytest.fixture
def store() -> StubStore:
    return StubStore()


@pytest.fixture
def broadcaster(store, clock) -> SnapshotBroadcaster:
    return SnapshotBroadcaster(store, clock)


async def taken(slot: SnapshotSlot) -> str | None:
    """The snapshot waiting in the slot, taken out; None if there is none.

    wait() returns without suspending when a snapshot is there, so a timeout that is
    already due tells "there is one" from "there is none" without waiting for real.
    (Not wait_for(timeout=0): it cancels the wait before it even starts.)
    """
    try:
        async with asyncio.timeout(0):
            return await slot.wait()
    except TimeoutError:
        return None


class TestTick:
    async def test_every_page_gets_the_same_string(self, broadcaster):
        slots = [broadcaster.subscribe() for _ in range(3)]

        assert await broadcaster.tick() is True

        first, *others = [await taken(slot) for slot in slots]
        assert isinstance(first, str)
        # Serialized once: the very same object, not three equal strings.
        assert all(text is first for text in others)

    async def test_snapshot_is_the_city_at_the_current_server_time(self, broadcaster, clock):
        slot = broadcaster.subscribe()

        await broadcaster.tick()

        expected = build_dispatcher_snapshot(CITY, clock.now_ms()).model_dump_json()
        assert json.loads(await taken(slot)) == json.loads(expected)

    async def test_tick_without_pages_keeps_nothing_for_the_next_one(self, broadcaster, store):
        # Not ticking without pages is the loop's job (next commit), not tick's.
        assert await broadcaster.tick() is True

        assert store.reads == 1
        assert await taken(broadcaster.subscribe()) is None


class TestSubscriptions:
    async def test_page_gets_the_last_snapshot_at_once(self, broadcaster):
        first = broadcaster.subscribe()
        await broadcaster.tick()
        snapshot = await taken(first)

        late = broadcaster.subscribe()

        assert await taken(late) == snapshot

    async def test_page_before_any_snapshot_gets_nothing_yet(self, broadcaster):
        slot = broadcaster.subscribe()

        assert await taken(slot) is None

    async def test_unsubscribed_page_gets_no_more_snapshots(self, broadcaster):
        gone = broadcaster.subscribe()
        stays = broadcaster.subscribe()
        broadcaster.unsubscribe(gone)

        await broadcaster.tick()

        assert await taken(gone) is None
        assert await taken(stays) is not None

    async def test_snapshot_is_dropped_when_the_last_page_leaves(self, broadcaster):
        # The next first page must get a fresh snapshot, not one from long ago.
        slot = broadcaster.subscribe()
        await broadcaster.tick()
        broadcaster.unsubscribe(slot)

        assert await taken(broadcaster.subscribe()) is None

    async def test_snapshot_read_while_the_last_page_left_is_not_kept(self, broadcaster, store):
        slot = broadcaster.subscribe()
        store.release = asyncio.Event()
        ticking = asyncio.create_task(broadcaster.tick())
        await let_tasks_run()  # the tick is now waiting for Redis

        broadcaster.unsubscribe(slot)
        store.release.set()
        await ticking

        assert await taken(broadcaster.subscribe()) is None

    def test_unsubscribing_twice_is_harmless(self, broadcaster):
        slot = broadcaster.subscribe()
        broadcaster.unsubscribe(slot)

        broadcaster.unsubscribe(slot)

        assert broadcaster.subscribers == 0

    def test_pages_above_the_limit_are_refused(self, broadcaster, monkeypatch):
        monkeypatch.setattr(config, "DISPATCHER_MAX_CONNECTIONS", 2)
        broadcaster.subscribe()
        broadcaster.subscribe()

        with pytest.raises(TooManySubscribers):
            broadcaster.subscribe()
        assert broadcaster.subscribers == 2

    def test_page_that_leaves_frees_its_place(self, broadcaster, monkeypatch):
        monkeypatch.setattr(config, "DISPATCHER_MAX_CONNECTIONS", 1)
        broadcaster.unsubscribe(broadcaster.subscribe())

        broadcaster.subscribe()

        assert broadcaster.subscribers == 1


class TestFailures:
    async def test_failed_tick_is_counted_and_pages_keep_the_last_snapshot(
        self, broadcaster, store
    ):
        slot = broadcaster.subscribe()
        await broadcaster.tick()
        last = await slot.wait()
        store.failing = True
        before = sample("dispatcher_snapshot_failures_total")

        assert await broadcaster.tick() is False

        assert sample("dispatcher_snapshot_failures_total") == before + 1
        assert await taken(slot) is None  # nothing new; the page still shows `last`
        # And a page that opens now gets that last snapshot too.
        assert await taken(broadcaster.subscribe()) == last

    async def test_next_tick_after_a_failure_succeeds(self, broadcaster, store):
        slot = broadcaster.subscribe()
        store.failing = True
        await broadcaster.tick()

        store.failing = False

        assert await broadcaster.tick() is True
        assert await taken(slot) is not None

    async def test_failures_are_logged_at_most_once_per_interval(
        self, broadcaster, store, clock, caplog
    ):
        store.failing = True

        await broadcaster.tick()
        clock.advance(config.DISPATCHER_FAILURE_LOG_INTERVAL_S - 1)
        await broadcaster.tick()
        assert caplog.text.count("Dispatcher snapshot failed") == 1

        clock.advance(1)
        await broadcaster.tick()

        assert caplog.text.count("Dispatcher snapshot failed") == 2
        assert {r.levelno for r in caplog.records} == {logging.WARNING}

    async def test_recovery_is_logged_once(self, broadcaster, store, caplog):
        caplog.set_level(logging.INFO)
        store.failing = True
        await broadcaster.tick()
        store.failing = False

        await broadcaster.tick()
        await broadcaster.tick()

        assert caplog.text.count("Dispatcher snapshots recovered") == 1

    async def test_healthy_ticks_log_nothing(self, broadcaster, caplog):
        caplog.set_level(logging.INFO)

        await broadcaster.tick()

        assert caplog.records == []

    async def test_new_run_of_failures_after_recovery_is_logged_at_once(
        self, broadcaster, store, caplog
    ):
        store.failing = True
        await broadcaster.tick()
        store.failing = False
        await broadcaster.tick()
        store.failing = True

        await broadcaster.tick()  # the clock has not moved

        assert caplog.text.count("Dispatcher snapshot failed") == 2


class TestMetrics:
    async def test_successful_tick_updates_the_snapshot_metrics(self, broadcaster, clock):
        slot = broadcaster.subscribe()
        builds = sample("dispatcher_snapshot_build_seconds_count")

        await broadcaster.tick()

        assert sample("dispatcher_snapshot_build_seconds_count") == builds + 1
        assert sample("dispatcher_snapshot_drivers") == 1
        assert sample("dispatcher_snapshot_bytes") == len((await taken(slot)).encode())

    async def test_failed_tick_leaves_the_snapshot_metrics_alone(self, broadcaster, store):
        await broadcaster.tick()
        builds = sample("dispatcher_snapshot_build_seconds_count")
        size = sample("dispatcher_snapshot_bytes")
        store.failing = True

        await broadcaster.tick()

        assert sample("dispatcher_snapshot_build_seconds_count") == builds
        assert sample("dispatcher_snapshot_bytes") == size
