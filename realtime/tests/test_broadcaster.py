"""Snapshots for the dispatcher map pages (app/ws/broadcaster.py): the latest-wins
slot of one page.

No timers: a waiting task is given a chance to run with yields to the event loop
(asyncio.sleep(0)), never with a real delay.
"""

import asyncio

from prometheus_client import REGISTRY

from app.ws.broadcaster import SnapshotSlot


def skipped() -> float:
    return REGISTRY.get_sample_value("dispatcher_snapshot_skipped_total") or 0.0


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
